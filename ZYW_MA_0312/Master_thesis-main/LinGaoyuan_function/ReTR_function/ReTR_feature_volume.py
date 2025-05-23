import numpy as np
import torch
from torch import nn
from einops import (rearrange, reduce, repeat)

from LinGaoyuan_function.ReTR_function.ReTR_grid_sample import grid_sample_2d
from LinGaoyuan_function.ReTR_function.ReTR_cnn3d import VolumeRegularization


class FeatureVolume(nn.Module):
    """
    Create the coarse feature volume in a MVS-like way
    """

    def __init__(self, volume_reso=100):
        """
        Set up the volume grid given resolution
        """
        super().__init__()

        self.volume_reso = volume_reso
        self.volume_regularization = VolumeRegularization()

        # the volume is a cube, so we only need to define the x, y, z
        x_line = (np.linspace(0, self.volume_reso - 1, self.volume_reso)) * 2 / (self.volume_reso - 1) - 1  # [-1, 1]
        y_line = (np.linspace(0, self.volume_reso - 1, self.volume_reso)) * 2 / (self.volume_reso - 1) - 1
        z_line = (np.linspace(0, self.volume_reso - 1, self.volume_reso)) * 2 / (self.volume_reso - 1) - 1
        self.multlevel = 3
        # create the volume grid
        self.x, self.y, self.z = np.meshgrid(x_line, y_line, z_line, indexing='ij')
        self.xyz = []
        for i in range(self.multlevel):
            level = 2 ** i
            self.xyz.append(np.stack([self.x[::level, ::level, ::level], self.y[::level, ::level, ::level],
                                      self.z[::level, ::level, ::level]]))

    def forward(self, feats, ray_batch):
        """
        feats: [B NV C H W], NV: number of views
        batch: to get the poses for homography
        """
        source_poses = (ray_batch['src_cameras'].squeeze())[:, -16:].reshape(-1, 4, 4)
        'LinGaoyuan_operation_20240917: add batch dim(1 by default in my code)'
        source_poses = source_poses[None,...]
        B, NV, _, _ = source_poses.shape
        # import pdb
        # pdb.set_trace()
        volume_mean_var_all = []
        for i in range(len(feats)):
            # ---- step 1: projection -----------------------------------------------
            volume_xyz_temp = torch.tensor(self.xyz[i]).type_as(source_poses)
            volume_xyz = volume_xyz_temp.reshape([3, -1])
            volume_xyz_homo = torch.cat([volume_xyz, torch.ones_like(volume_xyz[0:1])], axis=0)  # [4,XYZ]

            volume_xyz_homo_NV = repeat(volume_xyz_homo, "Num4 XYZ -> B NV Num4 XYZ", B=B, NV=NV)

            # volume project into views
            volume_xyz_pixel_homo = source_poses @ volume_xyz_homo_NV  # B NV 4 4 @ B NV 4 XYZ
            volume_xyz_pixel_homo = volume_xyz_pixel_homo[:, :, :3]
            mask_valid_depth = volume_xyz_pixel_homo[:, :, 2] > 0  # B NV XYZ
            mask_valid_depth = mask_valid_depth.float()
            mask_valid_depth = rearrange(mask_valid_depth, "B NV XYZ -> (B NV) XYZ")

            volume_xyz_pixel = volume_xyz_pixel_homo / volume_xyz_pixel_homo[:, :, 2:3]
            volume_xyz_pixel = volume_xyz_pixel[:, :, :2]
            volume_xyz_pixel = rearrange(volume_xyz_pixel, "B NV Dim2 XYZ -> (B NV) XYZ Dim2")
            volume_xyz_pixel = volume_xyz_pixel.unsqueeze(2)

            # projection: project all x * y * z points to NV images and sample features
            # grid sample 2D
            volume_feature, mask = grid_sample_2d(rearrange(feats[i][None,...], "B NV C H W -> (B NV) C H W"),
                                                  volume_xyz_pixel)  # (B NV) C XYZ 1, (B NV XYZ 1)

            volume_feature = volume_feature.squeeze(-1)
            mask = mask.squeeze(-1)  # (B NV XYZ)
            mask = mask * mask_valid_depth
            volume_feature = rearrange(volume_feature, "(B NV) C (NumX NumY NumZ) -> B NV NumX NumY NumZ C", B=B, NV=NV,
                                       NumX=self.xyz[i].shape[1], NumY=self.xyz[i].shape[2], NumZ=self.xyz[i].shape[3])
            mask = rearrange(mask, "(B NV) (NumX NumY NumZ) -> B NV NumX NumY NumZ", B=B, NV=NV,
                             NumX=self.xyz[i].shape[1], NumY=self.xyz[i].shape[2], NumZ=self.xyz[i].shape[3])

            weight = mask / (torch.sum(mask, dim=1, keepdim=True) + 1e-8)
            weight = weight.unsqueeze(-1)  # B NV X Y Z 1

            # ---- step 3: mean, var ------------------------------------------------
            mean = torch.sum(volume_feature * weight, dim=1, keepdim=True)  # B 1 X Y Z C
            var = torch.sum(weight * (volume_feature - mean) ** 2, dim=1, keepdim=True)  # B 1 X Y Z C
            mean = mean.squeeze(1)
            var = var.squeeze(1)
            # volume_pe = self.linear_pe[i](volume_xyz_temp.permute(3,2,1,0)).unsqueeze(0)
            volume_mean_var = torch.cat([mean, var], axis=-1)  # [B X Y Z C]
            volume_mean_var = volume_mean_var.permute(0, 4, 3, 2, 1)  # [B,C,Z,Y,X]
            volume_mean_var_all.append(volume_mean_var)
        # ---- step 4: 3D regularization ----------------------------------------
        volume_mean_var_reg = self.volume_regularization(volume_mean_var_all)

        return volume_mean_var_reg