import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import (rearrange, reduce, repeat)

from LinGaoyuan_function.ReTR_function.ReTR_grid_sample import grid_sample_2d, grid_sample_3d
from LinGaoyuan_function.ReTR_function.ReTR_transformer import LocalFeatureTransformer
from LinGaoyuan_function.ReTR_function.ReTR_cnn2d import ResidualBlock
import math


class LinGaoyuan_ReTR_model(nn.Module):
    def __init__(self, args, in_feat_ch=32, posenc_dim=3, viewenc_dim=3, ret_alpha=False, use_volume_feature = False, dim_clip_shape_code = 0):
        super().__init__()

        self.args = args
        self.in_feat_ch = in_feat_ch
        self.posenc_dim = posenc_dim
        self.ret_alpha = ret_alpha
        self.PE_d_hid = 8
        self.use_volume_feature = use_volume_feature

        'LinGaoyuan_20240930: define dimension of clip latent code'
        self.dim_clip_appearance_code = 128
        self.dim_clip_shape_code = dim_clip_shape_code

        'LinGaoyuan_20240915: 3 Transformer network in ReTR'
        self.view_transformer = LocalFeatureTransformer(d_model=self.in_feat_ch, nhead=8, layer_names=['self'],
                                                        attention='linear')

        if self.use_volume_feature and self.args.use_retr_feature_extractor:
            self.occu_transformer = LocalFeatureTransformer(d_model=self.in_feat_ch * 2 + self.PE_d_hid, nhead=8, layer_names=['self'],
                                                            attention='full')

            '''
            LinGaoyuan_20240930: add dimension of shape code, if the model is used for building and street, the self.dim_clip_shape_code = 0, 
            and if model is used for car, self.dim_clip_shape_code = 128
            '''
            self.ray_transformer = LocalFeatureTransformer(d_model=self.in_feat_ch * 2 + self.PE_d_hid + self.dim_clip_shape_code, nhead=1, layer_names=['cross'],
                                                           attention='full')

            'LinGaoyuan_20240915: MLP network in ReTR'

            '''
            LinGaoyuan_20240930: add dimension of clip appearance code to radiance mlp. 
            if the model is used for building and street, the self.dim_clip_shape_code = 0, 
            if model is used for car, self.dim_clip_shape_code = 128
            '''
            self.RadianceMLP = nn.Sequential(
                nn.Linear(self.in_feat_ch * 2 + self.PE_d_hid + self.dim_clip_appearance_code + self.dim_clip_shape_code, 32), nn.ReLU(inplace=True),
                nn.Linear(32, 16), nn.ReLU(inplace=True),
                nn.Linear(16, 3)
            )

            self.linear_radianceweight_1_softmax = nn.Sequential(
                nn.Linear(self.in_feat_ch+3, 16), nn.ReLU(inplace=True),
                nn.Linear(16, 8), nn.ReLU(inplace=True),
                nn.Linear(8, 1),
            )
            self.RadianceToken = ViewTokenNetwork(dim=self.in_feat_ch * 2 + self.PE_d_hid)
        else:
            self.occu_transformer = LocalFeatureTransformer(d_model=self.in_feat_ch + self.PE_d_hid, nhead=8, layer_names=['self'],
                                                            attention='full')
            '''
            LinGaoyuan_20240930: add dimension of shape code, if the model is used for building and street, the self.dim_clip_shape_code = 0, 
            and if model is used for car, self.dim_clip_shape_code = 128
            '''
            self.ray_transformer = LocalFeatureTransformer(d_model=self.in_feat_ch + self.PE_d_hid + self.dim_clip_shape_code, nhead=1, layer_names=['cross'],
                                                           attention='full')

            'LinGaoyuan_20240915: MLP network in ReTR'
            'LinGaoyuan_20240930: add dimension of clip appearance code to radiance mlp'
            self.RadianceMLP = nn.Sequential(
                nn.Linear(self.in_feat_ch + self.PE_d_hid + self.dim_clip_appearance_code + self.dim_clip_shape_code, 32), nn.ReLU(inplace=True),
                nn.Linear(32, 16), nn.ReLU(inplace=True),
                nn.Linear(16, 3)
            )
            'LinGaoyuan_operation_20240917: change the input dim of self.linear_radianceweight_1_softmax to self.in_feat_ch+3 when use_volume_feature is False'
            self.linear_radianceweight_1_softmax = nn.Sequential(
                nn.Linear(self.in_feat_ch+3, 16), nn.ReLU(inplace=True),
                nn.Linear(16, 8), nn.ReLU(inplace=True),
                nn.Linear(8, 1),
            )
            self.RadianceToken = ViewTokenNetwork(dim=self.in_feat_ch + self.PE_d_hid)

        self.fuse_layer = nn.Linear(self.in_feat_ch + 3, self.in_feat_ch)

        self.rgbfeat_fc = nn.Linear(self.in_feat_ch + 3, self.in_feat_ch)

        self.softmax = nn.Softmax(dim=-2)

        self.div_term = torch.exp((torch.arange(0, self.PE_d_hid, 2, dtype=torch.float) *
                            -(math.log(10000.0) / self.PE_d_hid)))

    def order_posenc(self, z_vals):
        """
        :param d_model: dimension of the model
        :param length: length of positions
        :return: length*d_model position matrix
        """
        if self.PE_d_hid  % 2 != 0:
            raise ValueError("Cannot use sin/cos positional encoding with "
                            "odd dim (got dim={:d})".format(self.PE_d_hid))
        pe = torch.zeros(z_vals.shape[0],z_vals.shape[1], self.PE_d_hid).to(z_vals.device)
        position = z_vals.unsqueeze(-1)
        # div_term = torch.exp((torch.arange(0, d_model, 2, dtype=torch.float) *
        #                     -(math.log(10000.0) / d_model))).to(z_vals.device)
        pe[:, :, 0::2] = torch.sin(position.float() * self.div_term.to(z_vals.device))
        pe[:, :, 1::2] = torch.cos(position.float() * self.div_term.to(z_vals.device))
        return pe
    def get_attn_mask(self, num_points):
        mask = (torch.triu(torch.ones(1, num_points+1, num_points+1)) == 1).transpose(1, 2)
    #    mask[:,0, 1:] = 0
        return mask
    def order_posenc(self, z_vals):
        """
        :param d_model: dimension of the model
        :param length: length of positions
        :return: length*d_model position matrix
        """
        if self.PE_d_hid  % 2 != 0:
            raise ValueError("Cannot use sin/cos positional encoding with "
                            "odd dim (got dim={:d})".format(self.PE_d_hid))
        pe = torch.zeros(z_vals.shape[0],z_vals.shape[1], self.PE_d_hid).to(z_vals.device)
        position = z_vals.unsqueeze(-1)
        # div_term = torch.exp((torch.arange(0, d_model, 2, dtype=torch.float) *
        #                     -(math.log(10000.0) / d_model))).to(z_vals.device)
        pe[:, :, 0::2] = torch.sin(position.float() * self.div_term.to(z_vals.device))
        pe[:, :, 1::2] = torch.cos(position.float() * self.div_term.to(z_vals.device))
        return pe

    'LinGaoyuan_20240930: retr model + model_and_model_component feature extractor or retr model + retr feature extractor'
    def forward(self, point3D, ray_batch, source_imgs_feat, z_vals, mask, ray_d, ray_diff, fea_volume=None, ret_alpha=True):

        B, n_views, H, W, rgb_channel = ray_batch['src_rgbs'].shape  # LinGaoyuan_20240916: B = Batch NV = num_source_views
        N_rand, N_samples, _ = point3D.shape

        img_rgb_sampled = source_imgs_feat[:, :, :, :3]  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 3)
        img_feat_sampled = source_imgs_feat[:,:,:,3:]  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 32)
        dir_relative = ray_diff[:,:,:,:3]

        # input_view = self.rgbfeat_fc(source_imgs_feat)  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 35) -> (N_rand, N_samples, n_views, 32)
        input_view = img_feat_sampled  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32)
        input_view = rearrange(input_view, 'N_rand N_samples n_views C -> (N_rand N_samples) n_views C')

        output_view = self.view_transformer(input_view)

        # output_view = output_view.permute(2,0,1,3)  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32) -> (n_views, N_rand, N_samples, 32)

        # input_occ = output_view[:,:,0,:]  # LinGaoyuan_20240916: (N_rand, N_samples, 1, 32)
        # view_feature = output_view[:,:,1:,:]  # LinGaoyuan_20240916: (N_rand, N_samples, NV - 1, 32)
        view_feature = output_view  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32)
        view_feature = rearrange(view_feature, '(N_rand N_samples) n_views C -> N_rand N_samples n_views C', N_rand = N_rand, N_samples = N_samples)

        x_weight = torch.cat([view_feature, dir_relative], dim=-1)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 35)
        x_weight = self.linear_radianceweight_1_softmax(x_weight)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 1)
        if x_weight.dtype == torch.float32:
            x_weight[mask==0] = -1e9
        else:
            x_weight[mask==0] = -1e4
        weight = self.softmax(x_weight)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 1)

        source_imgs_feat = rearrange(source_imgs_feat, "N_rand N_samples n_views C -> n_views C N_rand N_samples")# Zhenyi Wan [2025/3/14] (n_views 35 N_rand N_samples)
        radiance = (source_imgs_feat * rearrange(weight,"N_rand N_samples n_views 1 -> n_views 1 N_rand N_samples", N_rand=N_rand, N_samples = N_samples)).sum(axis=0)# Zhenyi Wan [2025/3/14] (35 N_rand N_samples)
        radiance = rearrange(radiance, "DimRGB N_rand N_samples -> N_rand N_samples DimRGB")# Zhenyi Wan [2025/3/14] (N_rand N_samples 35)

        attn_mask = self.get_attn_mask(N_samples).type_as(radiance)# Zhenyi Wan [2025/3/14] (1, N_samples+1, N_samples+1)
        input_occ = torch.cat((self.fuse_layer(radiance), self.order_posenc(100 * z_vals.reshape(-1,z_vals.shape[-1])).type_as(radiance)), dim=-1)# Zhenyi Wan [2025/3/14] (N_rand, N_samples, 40)
        radiance_tokens = self.RadianceToken(input_occ).unsqueeze(1)# Zhenyi Wan [2025/3/14] (N_rand, 1, 1, 40)
        input_occ = torch.cat((radiance_tokens, input_occ), dim=1)# Zhenyi Wan [2025/3/14] (N_rand, N_samples + 1, 40)

        output_occ = self.occu_transformer(input_occ)# Zhenyi Wan [2025/3/14] (N_rand, N_samples + 1, 40)

        output_ray = self.ray_transformer(output_occ[:,:1], output_occ[:,1:])# Zhenyi Wan [2025/3/20] query:(N_rand, 1, 40)
        # key,value:(N_rand, N_samples, 40). output is (N_rand, 1, 40)
        weight = self.ray_transformer.atten_weight.squeeze()# Zhenyi Wan [2025/3/20] (N_rand, N_samples)

        rgb = torch.sigmoid(self.RadianceMLP(output_ray))# Zhenyi Wan [2025/3/20] (N_rand, 1, 3)

        if len(rgb.shape) == 3:
            rgb = rgb.squeeze()# Zhenyi Wan [2025/3/24] (N_rand, 3)

        if ret_alpha  is True:
            rgb = torch.cat([rgb,weight], dim=1)# Zhenyi Wan [2025/3/24] (N_rand, N_samples+3)

        return rgb

    def forward_clip(self, point3D, ray_batch, source_imgs_feat, z_vals, mask, ray_d, ray_diff, fea_volume=None, ret_alpha=True, latent_code=None):

        B, n_views, H, W, rgb_channel = ray_batch['src_rgbs'].shape  # LinGaoyuan_20240916: B = Batch NV = num_source_views
        N_rand, N_samples, _ = point3D.shape

        img_rgb_sampled = source_imgs_feat[:, :, :, :3]  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 3)
        img_feat_sampled = source_imgs_feat[:,:,:,3:]  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 32)
        dir_relative = ray_diff[:,:,:,:3]

        'LinGaoyuan_20240930: create clip shape code and appearance code, the size of latent_code should be (128) or (2,128)'
        if (latent_code.shape)[0] == 2:
            clip_shape_code = latent_code[0,:]
            clip_appearance_code = latent_code[1,:]
        else:
            clip_shape_code = None
            clip_appearance_code = latent_code.unsqueeze(0)  # we want (128) -> (1,128)

        # input_view = self.rgbfeat_fc(source_imgs_feat)  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 35) -> (N_rand, N_samples, n_views, 32)
        input_view = img_feat_sampled  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32)
        input_view = rearrange(input_view, 'N_rand N_samples n_views C -> (N_rand N_samples) n_views C')

        output_view = self.view_transformer(input_view)

        # output_view = output_view.permute(2,0,1,3)  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32) -> (n_views, N_rand, N_samples, 32)

        # input_occ = output_view[:,:,0,:]  # LinGaoyuan_20240916: (N_rand, N_samples, 1, 32)
        # view_feature = output_view[:,:,1:,:]  # LinGaoyuan_20240916: (N_rand, N_samples, NV - 1, 32)
        view_feature = output_view  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32)
        view_feature = rearrange(view_feature, '(N_rand N_samples) n_views C -> N_rand N_samples n_views C', N_rand = N_rand, N_samples = N_samples)

        x_weight = torch.cat([view_feature, dir_relative], dim=-1)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 35)
        x_weight = self.linear_radianceweight_1_softmax(x_weight)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 1)
        if x_weight.dtype == torch.float32:
            x_weight[mask==0] = -1e9
        else:
            x_weight[mask==0] = -1e4
        weight = self.softmax(x_weight)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 1)

        source_imgs_feat = rearrange(source_imgs_feat, "N_rand N_samples n_views C -> n_views C N_rand N_samples")
        radiance = (source_imgs_feat * rearrange(weight,"N_rand N_samples n_views 1 -> n_views 1 N_rand N_samples", N_rand=N_rand, N_samples = N_samples)).sum(axis=0)
        radiance = rearrange(radiance, "DimRGB N_rand N_samples -> N_rand N_samples DimRGB")

        attn_mask = self.get_attn_mask(N_samples).type_as(radiance)
        input_occ = torch.cat((self.fuse_layer(radiance), self.order_posenc(100 * z_vals.reshape(-1,z_vals.shape[-1])).type_as(radiance)), dim=-1)
        radiance_tokens = self.RadianceToken(input_occ).unsqueeze(1)
        input_occ = torch.cat((radiance_tokens, input_occ), dim=1)

        output_occ = self.occu_transformer(input_occ)


        'LinGaoyuan_operation_20240930: determine to whether add the clip shape code to raY_transformer or not based on the size of latent_code'
        if clip_shape_code == None:
            output_ray = self.ray_transformer(output_occ[:,:1], output_occ[:,1:])
        else:
            clip_shape_code = clip_shape_code.repeat((output_occ.shape)[0], (output_occ.shape)[1], 1)
            output_occ = torch.cat((output_occ,clip_shape_code), dim=-1)
            output_ray = self.ray_transformer(output_occ[:, :1], output_occ[:, 1:])

        weight = self.ray_transformer.atten_weight.squeeze()

        'LinGaoyuan_20240930: cat clip radiance code as the input of self.RadianceMLP()'
        clip_appearance_code = clip_appearance_code.repeat((output_ray.shape)[0], (output_ray.shape)[1], 1)
        output_ray = torch.cat((output_ray,clip_appearance_code), dim=-1)

        rgb = torch.sigmoid(self.RadianceMLP(output_ray))

        if len(rgb.shape) == 3:
            rgb = rgb.squeeze()

        if ret_alpha  is True:
            rgb = torch.cat([rgb,weight], dim=1)

        return rgb

    'LinGaoyuan_operation_20240919: create new function to test the combination of retr feature extractor + model_and_model_component projector + retr volume feature'
    'LinGaoyuan_20240930: retr model + retr feature extractor + feature volume'
    def forward_retr(self, point3D, ray_batch, source_imgs_feat, z_vals, mask, ray_d, ray_diff, fea_volume=None, ret_alpha=True, latent_code=None):

        B, n_views, H, W, rgb_channel = ray_batch['src_rgbs'].shape  # LinGaoyuan_20240916: B = Batch NV = num_source_views
        N_rand, N_samples, _ = point3D.shape

        img_rgb_sampled = source_imgs_feat[:, :, :, :3]  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 3)
        img_feat_sampled = source_imgs_feat[:,:,:,3:]  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 32)
        dir_relative = ray_diff[:,:,:,:3]

        if fea_volume is not None:
            fea_volume_feat = grid_sample_3d(fea_volume, point3D[None,None,...].float())
            fea_volume_feat = rearrange(fea_volume_feat, "B C RN SN -> (B RN SN) C")

        # input_view = self.rgbfeat_fc(source_imgs_feat)  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 35) -> (N_rand, N_samples, n_views, 32)
        input_view = img_feat_sampled  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32)
        input_view = rearrange(input_view, 'N_rand N_samples n_views C -> (N_rand N_samples) n_views C')

        if fea_volume is not None:
            input_view = torch.cat((fea_volume_feat.unsqueeze(1), input_view), dim=1)

        output_view = self.view_transformer(input_view)

        # output_view = output_view.permute(2,0,1,3)  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32) -> (n_views, N_rand, N_samples, 32)

        input_occ = output_view[:,0,:]  # LinGaoyuan_20240916: (N_rand, N_samples, 1, 32)
        view_feature = output_view[:,1:,:]  # LinGaoyuan_20240916: (N_rand, N_samples, NV - 1, 32)
        # view_feature = output_view  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32)
        view_feature = rearrange(view_feature, '(N_rand N_samples) n_views C -> N_rand N_samples n_views C', N_rand = N_rand, N_samples = N_samples)

        x_weight = torch.cat([view_feature, dir_relative], dim=-1)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 35)
        x_weight = self.linear_radianceweight_1_softmax(x_weight)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 1)
        if x_weight.dtype == torch.float32:
            x_weight[mask==0] = -1e9
        else:
            x_weight[mask==0] = -1e4
        weight = self.softmax(x_weight)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 1)

        source_imgs_feat = rearrange(source_imgs_feat, "N_rand N_samples n_views C -> n_views C N_rand N_samples")
        radiance = (source_imgs_feat * rearrange(weight,"N_rand N_samples n_views 1 -> n_views 1 N_rand N_samples", N_rand=N_rand, N_samples = N_samples)).sum(axis=0)
        radiance = rearrange(radiance, "DimRGB N_rand N_samples -> N_rand N_samples DimRGB")

        input_occ = rearrange(input_occ, "(N_rand N_samples) C -> N_rand N_samples C", N_rand=N_rand, N_samples = N_samples)
        attn_mask = self.get_attn_mask(N_samples).type_as(radiance)
        input_occ = torch.cat((input_occ, self.fuse_layer(radiance), self.order_posenc(100 * z_vals.reshape(-1,z_vals.shape[-1])).type_as(radiance)), dim=-1)
        radiance_tokens = self.RadianceToken(input_occ).unsqueeze(1)
        input_occ = torch.cat((radiance_tokens, input_occ), dim=1)

        output_occ = self.occu_transformer(input_occ)  # output_occ: (N_rand, N_sample+1, 72), input_occ: (N_rand, N_sample+1, 72)

        output_ray = self.ray_transformer(output_occ[:,:1], output_occ[:,1:])  # output_ray: (N_rand, 1, 72)
        weight = self.ray_transformer.atten_weight.squeeze()

        rgb = torch.sigmoid(self.RadianceMLP(output_ray))

        if len(rgb.shape) == 3:
            rgb = rgb.squeeze()

        if ret_alpha  is True:
            rgb = torch.cat([rgb,weight], dim=1)

        return rgb

    'LinGaoyuan_operation_20240924: add clip module to retr model'
    'LinGaoyuan_20240930: retr model + retr feature extractor + feature volume'
    def forward_retr_clip(self, point3D, ray_batch, source_imgs_feat, z_vals, mask, ray_d, ray_diff, fea_volume=None, ret_alpha=True, latent_code=None):

        B, n_views, H, W, rgb_channel = ray_batch['src_rgbs'].shape  # LinGaoyuan_20240916: B = Batch NV = num_source_views
        N_rand, N_samples, _ = point3D.shape

        img_rgb_sampled = source_imgs_feat[:, :, :, :3]  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 3)
        img_feat_sampled = source_imgs_feat[:,:,:,3:]  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 32)
        dir_relative = ray_diff[:,:,:,:3]

        'LinGaoyuan_20240930: create clip shape code and appearance code, the size of latent_code should be (128) or (2,128)'
        if (latent_code.shape)[0] == 2:
            clip_shape_code = latent_code[0,:]
            clip_appearance_code = latent_code[1,:]
        else:
            clip_shape_code = None
            clip_appearance_code = latent_code.unsqueeze(0)  # we want (128) -> (1,128)


        if fea_volume is not None:
            fea_volume_feat = grid_sample_3d(fea_volume, point3D[None,None,...].float())
            fea_volume_feat = rearrange(fea_volume_feat, "B C RN SN -> (B RN SN) C")

        # input_view = self.rgbfeat_fc(source_imgs_feat)  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 35) -> (N_rand, N_samples, n_views, 32)
        input_view = img_feat_sampled  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32)
        input_view = rearrange(input_view, 'N_rand N_samples n_views C -> (N_rand N_samples) n_views C')

        if fea_volume is not None:
            input_view = torch.cat((fea_volume_feat.unsqueeze(1), input_view), dim=1)

        output_view = self.view_transformer(input_view)

        # output_view = output_view.permute(2,0,1,3)  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32) -> (n_views, N_rand, N_samples, 32)

        input_occ = output_view[:,0,:]  # LinGaoyuan_20240916: (N_rand, N_samples, 1, 32)
        view_feature = output_view[:,1:,:]  # LinGaoyuan_20240916: (N_rand, N_samples, NV - 1, 32)
        # view_feature = output_view  # LinGaoyuan_20240916: (N_rand, N_samples, n_views, 32)
        view_feature = rearrange(view_feature, '(N_rand N_samples) n_views C -> N_rand N_samples n_views C', N_rand = N_rand, N_samples = N_samples)

        x_weight = torch.cat([view_feature, dir_relative], dim=-1)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 35)
        x_weight = self.linear_radianceweight_1_softmax(x_weight)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 1)
        if x_weight.dtype == torch.float32:
            x_weight[mask==0] = -1e9
        else:
            x_weight[mask==0] = -1e4
        weight = self.softmax(x_weight)  # LinGaoyuan_20240917: (N_rand, N_samples, n_views, 1)

        source_imgs_feat = rearrange(source_imgs_feat, "N_rand N_samples n_views C -> n_views C N_rand N_samples")
        radiance = (source_imgs_feat * rearrange(weight,"N_rand N_samples n_views 1 -> n_views 1 N_rand N_samples", N_rand=N_rand, N_samples = N_samples)).sum(axis=0)
        radiance = rearrange(radiance, "DimRGB N_rand N_samples -> N_rand N_samples DimRGB")

        input_occ = rearrange(input_occ, "(N_rand N_samples) C -> N_rand N_samples C", N_rand=N_rand, N_samples = N_samples)
        attn_mask = self.get_attn_mask(N_samples).type_as(radiance)
        input_occ = torch.cat((input_occ, self.fuse_layer(radiance), self.order_posenc(100 * z_vals.reshape(-1,z_vals.shape[-1])).type_as(radiance)), dim=-1)
        radiance_tokens = self.RadianceToken(input_occ).unsqueeze(1)
        input_occ = torch.cat((radiance_tokens, input_occ), dim=1)

        output_occ = self.occu_transformer(input_occ)  # output_occ: (N_rand, N_sample+1, 72), input_occ: (N_rand, N_sample+1, 72)

        'LinGaoyuan_operation_20240930: determine to whether add the clip shape code to ray_transformer or not based on the size of latent_code'
        # output_ray = self.ray_transformer(output_occ[:,:1], output_occ[:,1:])  # output_ray: (N_rand, 1, 72)
        if clip_shape_code == None:
            output_ray = self.ray_transformer(output_occ[:,:1], output_occ[:,1:])
        else:
            clip_shape_code = clip_shape_code.repeat((output_occ.shape)[0], (output_occ.shape)[1], 1)
            output_occ = torch.cat((output_occ,clip_shape_code), dim=-1)
            output_ray = self.ray_transformer(output_occ[:, :1], output_occ[:, 1:])

        weight = self.ray_transformer.atten_weight.squeeze()

        'LinGaoyuan_20240930: cat clip radiance code as the input of self.RadianceMLP()'
        clip_appearance_code = clip_appearance_code.repeat((output_ray.shape)[0], (output_ray.shape)[1], 1)
        output_ray = torch.cat((output_ray,clip_appearance_code), dim=-1)

        rgb = torch.sigmoid(self.RadianceMLP(output_ray))

        if len(rgb.shape) == 3:
            rgb = rgb.squeeze()

        if ret_alpha  is True:
            rgb = torch.cat([rgb,weight], dim=1)

        return rgb

    def forward_retr_original(self, point3D, ray_batch, source_imgs_feat, z_vals, fea_volume=None, ret_alpha=True):

        B, NV, H, W, rgb_channel = ray_batch['src_rgbs'].shape  # B = 1
        RN, SN, _ = point3D.shape

        target_img_pose = (ray_batch['camera'].squeeze())[-16:].reshape(-1, 4, 4)
        source_imgs_pose = (ray_batch['src_cameras'].squeeze())[:, -16:].reshape(-1, 4, 4)
        source_imgs_intrinsics = (ray_batch['src_cameras'].squeeze())[:, 2:18].reshape(-1, 4, 4)
        source_imgs_pose = source_imgs_intrinsics.bmm(torch.inverse(source_imgs_pose))

        'LinGaoyuan_operation_20240916: add a batch diemension to some variable, B = 1 in this code'
        point3D = point3D[None, ...]
        source_imgs_pose = source_imgs_pose[None, ...]
        source_imgs_feat = source_imgs_feat[None, ...]
        source_imgs_rgb = ray_batch['src_rgbs']

        vector_1 = (point3D - repeat(torch.inverse(target_img_pose)[:,:3,-1], "B DimX -> B 1 1 DimX"))
        vector_1 = repeat(vector_1, "B RN SN DimX -> B 1 RN SN DimX")
        # vector_2 = (point3D.unsqueeze(1) - repeat(source_imgs_pose[:,:,:3,-1], "B L DimX -> B L 1 1 DimX"))
        vector_2 = (point3D.unsqueeze(1) - repeat(torch.inverse(source_imgs_pose)[:, :, :3, -1], "B L DimX -> B L 1 1 DimX"))
        vector_1 = vector_1/torch.linalg.norm(vector_1, dim=-1, keepdim=True) # normalize to get direction
        vector_2 = vector_2/torch.linalg.norm(vector_2, dim=-1, keepdim=True)
        dir_relative = vector_1 - vector_2
        dir_relative = dir_relative.float()


        if fea_volume is not None:
            fea_volume_feat = grid_sample_3d(fea_volume, point3D.unsqueeze(1).float())
            fea_volume_feat = rearrange(fea_volume_feat, "B C RN SN -> (B RN SN) C")
        # -------- project points to feature map
        # B NV RN SN CN DimXYZ
        point3D = repeat(point3D, "B RN SN DimX -> B NV RN SN DimX", NV=NV).float()
        point3D = torch.cat([point3D, torch.ones_like(point3D[:,:,:,:,:1])], axis=4)

        # B NV 4 4 -> (B NV) 4 4
        points_in_pixel = torch.bmm(rearrange(source_imgs_pose, "B NV M_1 M_2 -> (B NV) M_1 M_2", M_1=4, M_2=4),
                                    rearrange(point3D, "B NV RN SN DimX -> (B NV) DimX (RN SN)"))

        points_in_pixel = rearrange(points_in_pixel, "(B NV) DimX (RN SN) -> B NV DimX RN SN", B=B, RN=RN)
        points_in_pixel = points_in_pixel[:, :, :3]

        # in 2D pixel coordinate
        mask_valid_depth = points_in_pixel[:,:,2]>0  #B NV RN SN
        mask_valid_depth = mask_valid_depth.float()
        points_in_pixel = points_in_pixel[:,:,:2] / points_in_pixel[:,:,2:3]

        img_feat_sampled, mask = grid_sample_2d(rearrange(source_imgs_feat, "B NV C H W -> (B NV) C H W"),
                                rearrange(points_in_pixel, "B NV Dim2 RN SN -> (B NV) RN SN Dim2"))
        img_rgb_sampled, _ = grid_sample_2d(rearrange(source_imgs_rgb, "B NV H W C -> (B NV) C H W"),
                                rearrange(points_in_pixel, "B NV Dim2 RN SN -> (B NV) RN SN Dim2"))

        mask = rearrange(mask, "(B NV) RN SN -> B NV RN SN", B=B)
        mask = mask * mask_valid_depth
        img_feat_sampled = rearrange(img_feat_sampled, "(B NV) C RN SN -> B NV C RN SN", B=B)
        img_rgb_sampled = rearrange(img_rgb_sampled, "(B NV) C RN SN -> B NV C RN SN", B=B)

        x = rearrange(img_feat_sampled, "B NV C RN SN -> (B RN SN) NV C")

        if fea_volume is not None:
            x = torch.cat((fea_volume_feat.unsqueeze(1), x), dim=1)

        x = self.view_transformer(x)

        x1 = rearrange(x, "B_RN_SN NV C -> NV B_RN_SN C")

        '''
        LinGaoyuan_operation_20240916: if use volume feature as the input in view transformer: set x=x1[0], set view_feature = x1[1:],
        if only use img_feat_sampled as the input for view transformer: no need to set x again, and set view_feature = x1
        '''
        if fea_volume is not None:
            x = x1[0] #reference
            view_feature = x1[1:]
            view_feature = rearrange(view_feature, "NV (B RN SN) C -> B RN SN NV C", B=B, RN=RN, SN=SN)
            dir_relative = rearrange(dir_relative, "B NV RN SN Dim3 -> B RN SN NV Dim3")

            x_weight = torch.cat([view_feature, dir_relative], axis=-1)
            x_weight = self.linear_radianceweight_1_softmax(x_weight)
            mask = rearrange(mask, "B NV RN SN -> B RN SN NV 1")

            if x_weight.dtype == torch.float32:
                x_weight[mask==0] = -1e9
            else:
                x_weight[mask==0] = -1e4
            weight = self.softmax(x_weight)
            radiance = (torch.cat((img_rgb_sampled,img_feat_sampled),dim=2) * rearrange(weight, "B RN SN L 1 -> B L 1 RN SN", B=B, RN=RN)).sum(axis=1)
            radiance = rearrange(radiance, "B DimRGB RN SN -> (B RN) SN DimRGB")

            # add positional encoding
            x = rearrange(x, "(B RN SN) C -> (B RN) SN C", RN=RN, B=B, SN=SN)
            attn_mask = self.get_attn_mask(SN).type_as(x)
            x = torch.cat(
                (x, self.fuse_layer(radiance), self.order_posenc(100 * z_vals.reshape(-1, z_vals.shape[-1])).type_as(x)),
                dim=-1)
        else:
            view_feature = x1
            view_feature = rearrange(view_feature, "NV (B RN SN) C -> B RN SN NV C", B=B, RN=RN, SN=SN)
            dir_relative = rearrange(dir_relative, "B NV RN SN Dim3 -> B RN SN NV Dim3")

            x_weight = torch.cat([view_feature, dir_relative], axis=-1)
            x_weight = self.linear_radianceweight_1_softmax(x_weight)
            mask = rearrange(mask, "B NV RN SN -> B RN SN NV 1")

            if x_weight.dtype == torch.float32:
                x_weight[mask == 0] = -1e9
            else:
                x_weight[mask == 0] = -1e4
            weight = self.softmax(x_weight)
            radiance = (torch.cat((img_rgb_sampled, img_feat_sampled), dim=2) * rearrange(weight,
                                                                                          "B RN SN L 1 -> B L 1 RN SN",
                                                                                          B=B, RN=RN)).sum(axis=1)
            radiance = rearrange(radiance, "B DimRGB RN SN -> (B RN) SN DimRGB")

            # add positional encoding

            attn_mask = self.get_attn_mask(SN).type_as(x)
            x = torch.cat((self.fuse_layer(radiance), self.order_posenc(100 * z_vals.reshape(-1, z_vals.shape[-1])).type_as(x)),
                dim=-1)

        radiance_tokens = self.RadianceToken(x).unsqueeze(1)
        x = torch.cat((radiance_tokens, x), dim=1)
        x = self.occu_transformer(x, mask0=attn_mask)

        # calculate weight using view transformers result
        x = self.ray_transformer(x[:, :1], x[:,1:])
        weights = self.ray_transformer.atten_weight.squeeze()

        rgb = torch.sigmoid(self.RadianceMLP(x))

        if len(rgb.shape) == 3:
            rgb = rgb.squeeze()

        if ret_alpha is True:
            rgb = torch.cat([rgb,weights], dim=1)

        return rgb


class ViewTokenNetwork(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.register_parameter('view_token', nn.Parameter(torch.randn([1,dim])))

    def forward(self, x):
        return torch.ones([len(x), 1]).type_as(x) * self.view_token