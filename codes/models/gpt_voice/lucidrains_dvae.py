import math
from math import sqrt

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from torch import einsum

from models.vqvae.vqvae import Quantize
from trainer.networks import register_model
from utils.util import opt_get


def default(val, d):
    return val if val is not None else d


def eval_decorator(fn):
    def inner(model, *args, **kwargs):
        was_training = model.training
        model.eval()
        out = fn(model, *args, **kwargs)
        model.train(was_training)
        return out
    return inner


class ResBlock(nn.Module):
    def __init__(self, chan, conv):
        super().__init__()
        self.net = nn.Sequential(
            conv(chan, chan, 3, padding = 1),
            nn.ReLU(),
            conv(chan, chan, 3, padding = 1),
            nn.ReLU(),
            conv(chan, chan, 1)
        )

    def forward(self, x):
        return self.net(x) + x


class DiscreteVAE(nn.Module):
    def __init__(
        self,
        positional_dims=2,
        num_tokens = 512,
        codebook_dim = 512,
        num_layers = 3,
        num_resnet_blocks = 0,
        hidden_dim = 64,
        channels = 3,
        smooth_l1_loss = False,
        straight_through = False,
        normalization = None, # ((0.5,) * 3, (0.5,) * 3),
        record_codes = False,
    ):
        super().__init__()
        assert num_layers >= 1, 'number of layers must be greater than or equal to 1'
        has_resblocks = num_resnet_blocks > 0

        self.num_tokens = num_tokens
        self.num_layers = num_layers
        self.straight_through = straight_through
        self.codebook = Quantize(codebook_dim, num_tokens)
        self.positional_dims = positional_dims

        assert positional_dims > 0 and positional_dims < 3  # This VAE only supports 1d and 2d inputs for now.
        if positional_dims == 2:
            conv = nn.Conv2d
            conv_transpose = nn.ConvTranspose2d
        else:
            conv = nn.Conv1d
            conv_transpose = nn.ConvTranspose1d

        enc_chans = [hidden_dim] * num_layers
        dec_chans = list(reversed(enc_chans))

        enc_chans = [channels, *enc_chans]

        dec_init_chan = codebook_dim if not has_resblocks else dec_chans[0]
        dec_chans = [dec_init_chan, *dec_chans]

        enc_chans_io, dec_chans_io = map(lambda t: list(zip(t[:-1], t[1:])), (enc_chans, dec_chans))

        enc_layers = []
        dec_layers = []

        for (enc_in, enc_out), (dec_in, dec_out) in zip(enc_chans_io, dec_chans_io):
            enc_layers.append(nn.Sequential(conv(enc_in, enc_out, 4, stride = 2, padding = 1), nn.ReLU()))
            dec_layers.append(nn.Sequential(conv_transpose(dec_in, dec_out, 4, stride = 2, padding = 1), nn.ReLU()))

        for _ in range(num_resnet_blocks):
            dec_layers.insert(0, ResBlock(dec_chans[1], conv))
            enc_layers.append(ResBlock(enc_chans[-1], conv))

        if num_resnet_blocks > 0:
            dec_layers.insert(0, conv(codebook_dim, dec_chans[1], 1))

        enc_layers.append(conv(enc_chans[-1], codebook_dim, 1))
        dec_layers.append(conv(dec_chans[-1], channels, 1))

        self.encoder = nn.Sequential(*enc_layers)
        self.decoder = nn.Sequential(*dec_layers)

        self.loss_fn = F.smooth_l1_loss if smooth_l1_loss else F.mse_loss

        # take care of normalization within class
        self.normalization = normalization
        self.record_codes = record_codes
        if record_codes:
            self.codes = torch.zeros((32768,), dtype=torch.long)
            self.code_ind = 0
        self.internal_step = 0

    def norm(self, images):
        if not self.normalization is not None:
            return images

        means, stds = map(lambda t: torch.as_tensor(t).to(images), self.normalization)
        arrange = 'c -> () c () ()' if self.positional_dims == 2 else 'c -> () c ()'
        means, stds = map(lambda t: rearrange(t, arrange), (means, stds))
        images = images.clone()
        images.sub_(means).div_(stds)
        return images

    def get_debug_values(self, step, __):
        if self.record_codes:
            # Report annealing schedule
            return {'histogram_codes': self.codes}
        else:
            return {}

    @torch.no_grad()
    @eval_decorator
    def get_codebook_indices(self, images):
        img = self.norm(images)
        logits = self.encoder(img).permute((0,2,3,1) if len(img.shape) == 4 else (0,2,1))
        sampled, commitment_loss, codes = self.codebook(logits)
        return codes

    def decode(
        self,
        img_seq
    ):
        image_embeds = self.codebook.embed_code(img_seq)
        b, n, d = image_embeds.shape

        kwargs = {}
        if self.positional_dims == 1:
            arrange = 'b n d -> b d n'
        else:
            h = w = int(sqrt(n))
            arrange = 'b (h w) d -> b d h w'
            kwargs = {'h': h, 'w': w}
        image_embeds = rearrange(image_embeds, arrange, **kwargs)
        images = self.decoder(image_embeds)
        return images

    def forward(
        self,
        img
    ):
        img = self.norm(img)
        logits = self.encoder(img).permute((0,2,3,1) if len(img.shape) == 4 else (0,2,1))
        sampled, commitment_loss, codes = self.codebook(logits)
        sampled = sampled.permute((0,3,1,2) if len(img.shape) == 4 else (0,2,1))
        out = self.decoder(sampled)

        # reconstruction loss
        recon_loss = self.loss_fn(img, out)

        # This is so we can debug the distribution of codes being learned.
        if self.record_codes and self.internal_step % 50 == 0:
            codes = codes.flatten()
            l = codes.shape[0]
            i = self.code_ind if (self.codes.shape[0] - self.code_ind) > l else self.codes.shape[0] - l
            self.codes[i:i+l] = codes.cpu()
            self.code_ind = self.code_ind + l
            if self.code_ind >= self.codes.shape[0]:
                self.code_ind = 0
        self.internal_step += 1

        return recon_loss, commitment_loss, out


@register_model
def register_lucidrains_dvae(opt_net, opt):
    return DiscreteVAE(**opt_get(opt_net, ['kwargs'], {}))


if __name__ == '__main__':
    #v = DiscreteVAE()
    #o=v(torch.randn(1,3,256,256))
    #print(o.shape)
    v = DiscreteVAE(channels=1, normalization=None, positional_dims=1, num_tokens=4096, codebook_dim=2048, hidden_dim=256)
    o=v(torch.randn(1,1,256))
    print(o[-1].shape)