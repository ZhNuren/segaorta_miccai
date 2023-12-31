from random import sample
from typing import Any, List
from einops import rearrange
from git import Tag
import pandas as pd

import torch
from torch import nn

import torch
from lightning import LightningModule
from torchmetrics.classification.accuracy import Accuracy
from torch.optim import Adam, AdamW, SGD
from torch.optim.lr_scheduler import MultiStepLR, CosineAnnealingWarmRestarts, ReduceLROnPlateau, CyclicLR

import torch.nn as nn
# import segmentation_models_pytorch as smp

# from src.models.components.losses import Dice_and_FocalLoss, DiceLoss, FocalLoss, BCELoss, Dice_and_BCELoss
from monai.losses import DiceCELoss

from src.models.components.metrics import dice, recall, precision
from src.models.components.models import BaselineUNet, FastSmoothSENormDeepUNet_supervision_skip_no_drop
from monai.networks.nets import UNETR, SwinUNETR, SegResNet
from sklearn.metrics import roc_auc_score
from monai.inferers import sliding_window_inference
from monai.metrics import DiceMetric
from monai.transforms import (
    AsDiscrete,
    Compose,
)
from monai.data import (
    decollate_batch,
)
from functools import partial

# torch.set_float32_matmul_precision('medium')

class SegaModule(LightningModule):
    """
    Example of LightningModule for MNIST classification.

    A LightningModule organizes your PyTorch code into 5 sections:
        - Computations (init).
        - Train loop (training_step)
        - Validation loop (validation_step)
        - Test loop (test_step)
        - Optimizers (configure_optimizers)

    Read the docs:
        https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html
    """

    def __init__(
        self,

        **kwargs
    ):
        super().__init__()

        # this line ensures params passed to LightningModule will be saved to ckpt
        # it also allows to access params with 'self.hparams' attribute
        self.save_hyperparameters()


       
        if self.hparams['model'] == 'unet':
            self.model = BaselineUNet(in_channels=1, n_cls=1, n_filters=24)

        elif self.hparams['model'] == 'senet':
            self.model = FastSmoothSENormDeepUNet_supervision_skip_no_drop(
                            in_channels=1, n_cls=1, n_filters=12, reduction=2)

        elif self.hparams['model'] == 'segresnet':
            self.model = SegResNet(
                            in_channels=1, out_channels=2)
            
        # elif self.hparams['model'] == 'unetr':
        #     self.model = UNETR(in_channels=1, 
        #                        out_channels=1, 
        #                        img_size=(144,144,144))
                               
        elif self.hparams['model'] == 'swin':
            self.model = SwinUNETR(
                                    img_size=(self.hparams['roi'][0],self.hparams['roi'][1],self.hparams['roi'][2]),
                                    in_channels=1,
                                    out_channels=2,
                                    feature_size=48)

        #     checkpoint = torch.load("/home/ikboljon.sobirov/data/shared/ikboljon.sobirov/rima/lightning-hydra-template/weights/swin_unetr.base_5000ep_f48_lr2e-4_pretrained.pt")
        #     state_dict = checkpoint['state_dict']
        #     for key in list(state_dict):
        #         state_dict[key.replace("module.", "swinViT.")] = state_dict.pop(key)

        #     self.model.load_state_dict(state_dict)
        #     self.model.out = nn.Conv3d(48, 1, kernel_size=(1, 1, 1), stride=(1, 1, 1))

        else:
            print('Please select the correct model architecture name.')
            
        self.loss_mask = DiceCELoss(to_onehot_y=True, sigmoid=True)
        # self.loss_mask = Dice_and_FocalLoss()

        self.validation_step_loss = []
        self.validation_step_dice = []
        # self.validation_step_recall = []
        # self.validation_step_precision = []

        self.model_inferer = partial(
                sliding_window_inference,
                roi_size=[self.hparams['roi'][0], self.hparams['roi'][1], self.hparams['roi'][2]],
                sw_batch_size=1,
                predictor=self.model,
                overlap=0.5,
            )
        
        self.post_label = Compose([AsDiscrete(to_onehot=2)])
        self.post_pred = Compose([AsDiscrete(argmax=True, to_onehot=2)])
        self.dice_acc = DiceMetric(include_background=False, get_not_nans=False)

        



    def forward(self, x: torch.Tensor):
        return self.model(x)

    def init_params(self, m: torch.nn.Module):
        """Initialize the parameters of a module.
        Parameters
        ----------
        m
            The module to initialize.
        Notes
        -----
        Convolutional layer weights are initialized from a normal distribution
        as described in [1]_ in `fan_in` mode. The final layer bias is
        initialized so that the expected predicted probability accounts for
        the class imbalance at initialization.
        References
        ----------
        .. [1] K. He et al. ‘Delving Deep into Rectifiers: Surpassing
           Human-Level Performance on ImageNet Classification’,
           arXiv:1502.01852 [cs], Feb. 2015.
        """

        if isinstance(m, nn.Conv3d):
            nn.init.kaiming_normal_(m.weight, a=.1)
        elif isinstance(m, nn.BatchNorm3d):
            nn.init.constant_(m.weight, 1.)
            nn.init.constant_(m.bias, 0.)
        elif isinstance(m, nn.Linear):
            nn.init.kaiming_normal_(m.weight)
            # initialize the final bias so that the predictied probability at
            # init is equal to the proportion of positive samples
            nn.init.constant_(m.bias, -1.5214691)


    def step(self, batch: Any):
        sample = batch
        
        # torch.stack((sample[0]['image'].squeeze(dim=0),sample[1]['image'].squeeze(dim=0),sample[2]['image'].squeeze(dim=0),sample[3]['image'].squeeze(dim=0)), dim=0)

        # input = torch.cat((sample[0]['image'],sample[1]['image'],sample[2]['image'],sample[3]['image']), dim=0)
        # target = torch.cat((sample[0]['label'],sample[1]['label'],sample[2]['label'],sample[3]['label']), dim=0)

        pred_mask = self.forward(sample['image'])

        loss = self.loss_mask(pred_mask, sample['label'])

        return loss, pred_mask, sample['label']

    def val_step(self, batch: Any):
        sample = batch
        pred_mask = self.model_inferer(sample['image'])
        pred_mask_list = decollate_batch(pred_mask)
        pred_mask_convert = [self.post_pred(pred_mask_tensor) for pred_mask_tensor in pred_mask_list]

        label_mask_list = decollate_batch(sample['label'])
        label_mask_convert = [self.post_label(label_mask_tensor) for label_mask_tensor in label_mask_list]
        
        loss = self.loss_mask(pred_mask_convert[0].unsqueeze(0), sample['label'])

        return loss, pred_mask_convert[0].unsqueeze(0), label_mask_convert[0].unsqueeze(0)

    def training_step(self, batch: Any, batch_idx: int):
        loss, pred_mask, target = self.step(batch)

        # log train metrics
        #acc = self.train_accuracy(preds, targets)
        self.log("train/loss", loss, on_step=False, on_epoch=True, prog_bar=False)
        #self.log("train/acc", acc, on_step=False, on_epoch=True, prog_bar=True)

        # we can return here dict with any tensors
        # and then read it in some callback or in training_epoch_end() below
        # remember to always return loss from training_step, or else backpropagation will fail!
        return {"loss": loss}

    def on_train_epoch_end(self):
        # `outputs` is a list of dicts returned from `training_step()`
        pass

    def validation_step(self, batch: Any, batch_idx: int):
        loss, pred_mask, target = self.val_step(batch)

        self.dice_acc.reset()
        dice_val = self.dice_acc(pred_mask, target)[0][0]

        # recall_val = recall(pred_mask, target)
        # precision_val = precision(pred_mask, target)

        self.validation_step_loss.append(loss)
        self.validation_step_dice.append(dice_val)
        # self.validation_step_recall.append(recall_val)
        # self.validation_step_precision.append(precision_val)
        

        return {"loss": loss, "pred_mask": pred_mask, "target": target}
        # pass

    def on_validation_epoch_end(self):
        # pass

        loss        = torch.stack(self.validation_step_loss).mean()
        dice_val    = torch.stack(self.validation_step_dice).mean()
        # recall_val  = torch.stack(self.validation_step_recall).mean()
        # precision_val = torch.stack(self.validation_step_precision).mean()
        # target   = torch.cat([x["target"] for x in outputs])
        # pred_mask   = torch.cat([x["pred_mask"] for x in outputs])

        # dice_val = dice(pred_mask, target)
        # recall_val = recall(pred_mask, target)
        # precision_val = precision(pred_mask, target)
        # rocauc = roc_auc_score(target, pred_mask)

        log = {"val/loss": loss,
               "val/dice": dice_val,
            #    "val/recall": recall_val,
            #    "val/precision": precision_val,
            #    "rocauc": rocauc
               }
        
        self.log_dict(log)


        self.validation_step_loss.clear()
        self.validation_step_dice.clear()
        # self.validation_step_recall.clear()
        # self.validation_step_precision.clear()

        # return {"loss": loss, "dice": dice_val, "recall": recall_val, "precision": precision_val}
        return {"loss": loss, "dice": dice_val}


        # pass

    # def test_step(self, batch: Any, batch_idx: int):
        

    #     # return self.validation_step(batch, batch_idx)
    #     pass

    # def test_epoch_end(self, outputs: List[Any]):


    #     pass

    def configure_optimizers(self):
        """Choose what optimizers and learning-rate schedulers to use in your optimization.
        Normally you'd need one. But in the case of GANs or similar you might have multiple.

        See examples here:
            https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#configure-optimizers
        """
        # optimizer = make_optimizer(AdamW, self.model, lr=self.hparams.lr, weight_decay=self.hparams.weight_decay)
        
        optimizer = AdamW(self.parameters(),
                         lr=self.hparams.lr)
        scheduler = {
            # "scheduler": CosineAnnealingWarmRestarts(optimizer, T_0=25, eta_min=1e-5),
            "scheduler": torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=self.hparams.max_epochs),
            # "scheduler": MultiStepLR(optimizer, milestones=[75], gamma=0.05),
            #"scheduler": CyclicLR(optimizer, base_lr=1e-5, max_lr=1e-2),
            # "scheduler": ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=10, verbose=True),
            "monitor": "val/loss",
        }
        return [optimizer], [scheduler]
    