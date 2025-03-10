import torch
# import copy
from torch import nn
from network.dama import DAMA
from network.tcm import TCM

class DeepfakeDetector(nn.Module):
    def __init__(self, in_channels=3, dama_dim=128, batch_size=16):
        """
        结合DAMA和TCM的深度伪造检测器
        
        :param in_channels: 输入视频帧的通道数
        :type in_channels: int
        :param dama_dim: DAMA的输入特征维度
        :type dama_dim: int
        :param fusion_type: 特征融合方式（'concat'/'add'）
        :type fusion_type: str
        """
        super().__init__()
        
        self.dama_dim = dama_dim
        self.in_channels = in_channels
        self.batch_size = batch_size
        
        # DAMA模块 - 提取关键帧的空间特征与时频特征
        self.dama = DAMA(in_channels=in_channels, dim=dama_dim, batch_size=batch_size)
        
        # TCM模块 - 分析视频帧序列的时序一致性
        self.tcm = TCM(dama_dim=dama_dim)
        
        # 特征融合层
        self.fusion_gate = nn.Sequential(
            nn.Linear(dama_dim*2, 2),
            nn.Softmax(dim=1)
        )
            
        # 分类层
        self.classifier = nn.Sequential(
            nn.Linear(dama_dim, 256),
            nn.ReLU(),
            nn.Dropout(0.5),
            nn.Linear(256, 2)
        )
        
        
    def forward(self, x, batch_size):
        """
        前向传播
        """
        B, T, C, H, W = x.shape
        device = x.device
        
        num_gpus = torch.cuda.device_count()
        if num_gpus <= 1:
            return self._forward_single_gpu(x, batch_size)
        
        frames_per_gpu = T // num_gpus
        remainder = T % num_gpus
        
        all_dama_feats = []
        start_idx = 0
        
        for gpu_id in range(num_gpus):
            current_frames = frames_per_gpu
            if gpu_id < remainder:
                current_frames += 1
            
            if current_frames <= 0:
                continue
            
            end_idx = start_idx + current_frames
            
            target_device = f"cuda:{gpu_id}"
            frames_subset = x[:, start_idx:end_idx].to(target_device)
            
            print(f"Processing frames {start_idx} to {end_idx} on GPU {gpu_id}...")
            with torch.cuda.device(target_device):
                local_dama = type(self.dama)(
                    in_channels=self.in_channels,
                    dim=self.dama_dim,
                    deform_groups=self.dama.deform_groups
                ).to(target_device)
                
                local_dama.load_state_dict(self.dama.state_dict())
                
                dama_result = local_dama(frames_subset, batch_size=batch_size)
                
                del local_dama
                torch.cuda.empty_cache()
            all_dama_feats.append(dama_result.to(device))
            start_idx = end_idx
            
        # 合并所有GPU的DAMA特征
        dama_feats = torch.mean(torch.stack(all_dama_feats, dim=0), dim=0)
        del all_dama_feats
        torch.cuda.empty_cache()
        
        # TCM分析时序一致性
        tcm_outputs = self.tcm(x, dama_feats)
        tcm_consistency = tcm_outputs['consistency_score']
        tcm_feats = tcm_outputs['tcm_features']
        
        # 分类
        gate = self.fusion_gate(torch.cat([dama_feats, tcm_feats], dim=-1))
        fused_feats = gate[:, 0].unsqueeze(-1) * dama_feats + gate[:, 1].unsqueeze(-1) * tcm_feats
        logits = self.classifier(fused_feats)
        
        return {
            'logits': logits,
            'dama_feats': dama_feats,
            'tcm_consistency': tcm_consistency
        }
        
    # def _process_dama_on_gpu(self, frame_subset, batch_size, gpu_id):
    #     target_device = f"cuda:{gpu_id}"
        
    #     dama = copy.deepcopy(self.dama).to(target_device)
        
    #     with torch.cuda.device(target_device):
    #         dama_feats = dama(frame_subset, batch_size=batch_size)
            
    #     for module in dama.modules():
    #         del module
    #     del dama
    #     torch.cuda.empty_cache()
        
    #     return dama_feats
        
    def _forward_single_gpu(self, x, batch_size):
        """
        单GPU前向传播
        """
        B, T, C, H, W = x.shape
        device = x.device
        
        # 1. DAMA处理帧序列
        dama_feats = self.dama(x, batch_size=batch_size)
        
        # 2. TCM分析时序一致性
        tcm_outputs = self.tcm(x, dama_feats)
        tcm_consistency = tcm_outputs['consistency_score'] # [B]
        tcm_feats = tcm_outputs['tcm_features'] # [B, T, D]
        
        # 3. 特征融合
        gate = self.fusion_gate(torch.cat([dama_feats, tcm_feats], dim=-1))
        fused_feats = gate[:, 0].unsqueeze(-1) * dama_feats + gate[:, 1].unsqueeze(-1) * tcm_feats
        
        # 4. 分类
        logits = self.classifier(fused_feats)
        
        return {
            'logits': logits,
            'dama_feats': dama_feats,
            'tcm_consistency': tcm_consistency
        }
        