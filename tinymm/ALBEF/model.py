import numpy as np
import torch
import torch.nn.functional as F

from torch import nn
from tinymm.utils import create_timm_model
from tinymm.model_config import ALBEFConfig
from tinymm.GPT.model import GPTConfig, GPT, Block


class ImageEncoder(nn.Module):
    vit_output_dims: int = 768

    def __init__(self, config: ALBEFConfig):
        super().__init__()

        base_model = create_timm_model(config)
        layers = list(base_model.children())[:-1]
        self.encoder = nn.Sequential(*layers)
        self.img_proj = nn.Linear(self.vit_output_dims, config.itc_embd)

    def get_num_params(self):
        n_params = sum(p.numel() for p in self.parameters())
        return n_params

    def forward(self, inp):
        out = self.encoder(inp)
        last_token = out[:, -1, :]
        return self.img_proj(last_token), out


class TextEncoder(nn.Module):

    def __init__(self, gconfig, config):
        super().__init__()
        self.encoder = GPT(gconfig)
        self.txt_proj = nn.Linear(config.text_embd, config.itc_embd)

    def get_num_params(self):
        n_params = sum(p.numel() for p in self.parameters())
        return n_params

    def forward(self, inp):
        out = self.encoder(inp)
        last_token = out[:, -1, :]
        return self.txt_proj(last_token), out


class ALBEF(nn.Module):
    def __init__(self, config):
        super().__init__()

        gconfig = GPTConfig(config)
        gconfig.is_causal = False  # Use bidirectional attention
        self.img_encoder = ImageEncoder(config)
        self.txt_encoder = TextEncoder(gconfig, config)
        print("Image Encoder number of parameters:", self.img_encoder.get_num_params())
        print("Text Encoder number of parameters:", self.txt_encoder.get_num_params())

        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
        self.multimodal_encoder = nn.ModuleList(
            [Block(gconfig) for _ in range(config.multimodal_layer)]
        )
        self.itm_mlp = nn.Linear(config.text_embd, 2)

    def forward(self, inp):
        images, texts, targets = inp
        _, text_seq_len = texts.size()

        # ITC loss
        img_embds, img_feature = self.img_encoder(images)
        txt_embds, txt_feature = self.txt_encoder(texts)
        img_embds = F.normalize(img_embds, dim=-1)
        txt_embds = F.normalize(txt_embds, dim=-1)

        # mainly learned from https://github.com/openai/CLIP/blob/main/clip/model.py
        logits_per_image = self.logit_scale.exp() * img_embds @ txt_embds.T
        logits_per_text = logits_per_image.T

        labels = torch.arange(logits_per_image.size(0), device=images.device)
        itc_loss = (
            F.cross_entropy(logits_per_image, labels)
            + F.cross_entropy(logits_per_text, labels)
        ) / 2.0

        out = torch.cat((img_feature, txt_feature), dim=1)
        for block in self.multimodal_encoder:
            out = block(out)  # B, S, E
        # ITM loss
        # last_token = out[:, -1, :]
        # itm_out = self.itm_mlp(last_token)  # B, S, 2
        # itm_loss = F.cross_entropy(itm_out, match_labels)
        # MLM loss
        logits = self.txt_encoder.encoder.lm_head(out[:, -text_seq_len:, :])
        mlm_loss = F.cross_entropy(
            logits.view(-1, logits.size(-1)),
            targets.view(-1),
            ignore_index=-1,
        )
        return (
            logits_per_image,
            logits_per_text,
            labels,
            logits,
            targets,
            itc_loss,
            mlm_loss,  # pylint: disable=duplicate-code
            itc_loss + mlm_loss,
        )


if __name__ == "__main__":
    config = ALBEFConfig()
    encoder = ImageEncoder(config)
    image = torch.rand([64, 3, 256, 256])
    tok, out = encoder(image)
    print("tok:", tok.size(), "out:", out.size())

    model = ALBEF(config)
    text = torch.rand([64, 64]) * 100
    print("text:", text.long())
    _, _, _, _, _, loss = model((image, text.long(), text.long()))
    print("loss:", loss)
