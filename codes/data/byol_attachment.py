import random
from time import time

import torch
import torchvision
from torch.utils.data import Dataset
from kornia import augmentation as augs
from kornia import filters
import torch.nn as nn
import torch.nn.functional as F

# Wrapper for a DLAS Dataset class that applies random augmentations from the BYOL paper to BOTH the 'lq' and 'hq'
# inputs. These are then outputted as 'aug1' and 'aug2'.
from tqdm import tqdm

from data import create_dataset
from models.archs.arch_util import PixelUnshuffle
from utils.util import opt_get


class RandomApply(nn.Module):
    def __init__(self, fn, p):
        super().__init__()
        self.fn = fn
        self.p = p
    def forward(self, x):
        if random.random() > self.p:
            return x
        return self.fn(x)


class ByolDatasetWrapper(Dataset):
    def __init__(self, opt):
        super().__init__()
        self.wrapped_dataset = create_dataset(opt['dataset'])
        self.cropped_img_size = opt['crop_size']
        augmentations = [ \
            RandomApply(augs.ColorJitter(0.8, 0.8, 0.8, 0.2), p=0.8),
            augs.RandomGrayscale(p=0.2),
            augs.RandomHorizontalFlip(),
            RandomApply(filters.GaussianBlur2d((3, 3), (1.5, 1.5)), p=0.1),
            augs.RandomResizedCrop((self.cropped_img_size, self.cropped_img_size))]
        if opt['normalize']:
            # The paper calls for normalization. Recommend setting true if you want exactly like the paper.
            augmentations.append(augs.Normalize(mean=torch.tensor([0.485, 0.456, 0.406]), std=torch.tensor([0.229, 0.224, 0.225])))
        self.aug = nn.Sequential(*augmentations)

    def __getitem__(self, item):
        item = self.wrapped_dataset[item]
        item.update({'aug1': self.aug(item['hq']).squeeze(dim=0), 'aug2': self.aug(item['lq']).squeeze(dim=0)})
        return item

    def __len__(self):
        return len(self.wrapped_dataset)


def no_batch_interpolate(i, size, mode):
    i = i.unsqueeze(0)
    i = F.interpolate(i, size=size, mode=mode)
    return i.squeeze(0)


# Performs a 1-d translation of "other":
# If other<ref, returns 0.
# Else: return other-ref
def snap(ref, other):
    if other < ref:
        return 0
    return other - ref


# Pads a tensor with zeros so that it fits in a dxd square.
def pad_to(im, d):
    if len(im.shape) == 3:
        pd = torch.zeros((im.shape[0],d,d))
        pd[:, :im.shape[1], :im.shape[2]] = im
    else:
        pd = torch.zeros((im.shape[0],im.shape[1],d,d), device=im.device)
        pd[:, :, :im.shape[2], :im.shape[3]] = im
    return pd


# Variation of RandomResizedCrop, which picks a region of the image that the two augments must share. The augments
# then propagate off random corners of the shared region, using the same scale.
#
# Operates in units of "multiple". The intent is that this multiple is equivalent to the compression multiple of the
# latent space being used so that each structural unit corresponds to a latent unit.
class RandomSharedRegionCrop(nn.Module):
    def __init__(self, multiple, jitter_range=0):
        super().__init__()
        self.multiple = multiple
        self.jitter_range = jitter_range  # When specified, images are shifted an additional random([-j,j]) pixels where j=jitter_range

    def forward(self, i1, i2):
        assert i1.shape[-1] == i2.shape[-1]
        # Outline of the general algorithm:
        # 1. Assume the input is a square. Divide it by self.multiple to get working units.
        # 2. Pick a random width, height and top corner location for the first patch.
        # 3. Pick a random width, height and top corner location for the second patch.
        #    Note: All dims from (2) and (3) must contain at least half of the image, guaranteeing overlap.
        # 6. Build patches from input images. Resize them appropriately. Apply translational jitter.
        # 7. Compute the metrics needed to extract overlapping regions from the resized patches: top, left,
        #    original_height, original_width.
        # 8. Compute the "shared_view" from the above data.

        # Step 1
        c, d, _ = i1.shape
        assert d % self.multiple == 0 and d > (self.multiple*3)
        d = d // self.multiple

        # Step 2
        base_w = random.randint(d//2, d-1)
        base_l = random.randint(0, d-base_w)
        base_h = random.randint(base_w-1, base_w+1)
        base_t = random.randint(0, d-base_h)
        base_r, base_b = base_l+base_w, base_t+base_h

        # Step 3
        im2_w = random.randint(d//2, d-1)
        im2_l = random.randint(0, d-im2_w)
        im2_h = random.randint(im2_w-1, im2_w+1)
        im2_t = random.randint(0, d-im2_h)
        im2_r, im2_b = im2_l+im2_w, im2_t+im2_h

        # Step 6
        m = self.multiple
        jl, jt = random.randint(-self.jitter_range, self.jitter_range), random.randint(-self.jitter_range, self.jitter_range)
        jt = jt if base_t != 0 else abs(jt)  # If the top of a patch is zero, a negative jitter will cause it to go negative.
        jt = jt if (base_t+base_h)*m != i1.shape[1] else 0 # Likewise, jitter shouldn't allow the patch to go over-bounds.
        jl = jl if base_l != 0 else abs(jl)
        jl = jl if (base_l+base_w)*m != i1.shape[1] else 0
        p1 = i1[:, base_t*m+jt:(base_t+base_h)*m+jt, base_l*m+jl:(base_l+base_w)*m+jl]
        p1_resized = no_batch_interpolate(p1, size=(d*m, d*m), mode="bilinear")
        jl, jt = random.randint(-self.jitter_range, self.jitter_range), random.randint(-self.jitter_range, self.jitter_range)
        jt = jt if im2_t != 0 else abs(jt)
        jt = jt if (im2_t+im2_h)*m != i2.shape[1] else 0
        jl = jl if im2_l != 0 else abs(jl)
        jl = jl if (im2_l+im2_w)*m != i2.shape[1] else 0
        p2 = i2[:, im2_t*m+jt:(im2_t+im2_h)*m+jt, im2_l*m+jl:(im2_l+im2_w)*m+jl]
        p2_resized = no_batch_interpolate(p2, size=(d*m, d*m), mode="bilinear")

        # Step 7
        i1_shared_t, i1_shared_l = snap(base_t, im2_t), snap(base_l, im2_l)
        i2_shared_t, i2_shared_l = snap(im2_t, base_t), snap(im2_l, base_l)
        ix_h = min(base_b, im2_b) - max(base_t, im2_t)
        ix_w = min(base_r, im2_r) - max(base_l, im2_l)
        recompute_package = torch.tensor([base_h, base_w, i1_shared_t, i1_shared_l, im2_h, im2_w, i2_shared_t, i2_shared_l, ix_h, ix_w], dtype=torch.long)

        # Step 8
        mask1 = torch.full((1, base_h*m, base_w*m), fill_value=.5)
        mask1[:, i1_shared_t*m:(i1_shared_t+ix_h)*m, i1_shared_l*m:(i1_shared_l+ix_w)*m] = 1
        masked1 = pad_to(p1 * mask1, d*m)
        mask2 = torch.full((1, im2_h*m, im2_w*m), fill_value=.5)
        mask2[:, i2_shared_t*m:(i2_shared_t+ix_h)*m, i2_shared_l*m:(i2_shared_l+ix_w)*m] = 1
        masked2 = pad_to(p2 * mask2, d*m)
        mask = torch.full((1, d*m, d*m), fill_value=.33)
        mask[:, base_t*m:(base_t+base_w)*m, base_l*m:(base_l+base_h)*m] += .33
        mask[:, im2_t*m:(im2_t+im2_w)*m, im2_l*m:(im2_l+im2_h)*m] += .33
        masked_dbg = i1 * mask

        return p1_resized, p2_resized, recompute_package, masked1, masked2, masked_dbg


# Uses the recompute package returned from the above dataset to extract matched-size "similar regions" from two feature
# maps.
def reconstructed_shared_regions(fea1, fea2, recompute_package: torch.Tensor):
    package = recompute_package.cpu()
    res1 = []
    res2 = []
    pad_dim = torch.max(package[:, -2:]).item()
    # It'd be real nice if we could do this at the batch level, but I don't see a really good way to do that outside
    # of conforming the recompute_package across the entire batch.
    for b in range(package.shape[0]):
        f1_h, f1_w, f1s_t, f1s_l, f2_h, f2_w, f2s_t, f2s_l, s_h, s_w = tuple(package[b].tolist())
        # Resize the input features to match
        f1s = F.interpolate(fea1[b].unsqueeze(0), (f1_h, f1_w), mode="bilinear")
        f2s = F.interpolate(fea2[b].unsqueeze(0), (f2_h, f2_w), mode="bilinear")
        # Outputs must be padded so they can "get along" with each other.
        res1.append(pad_to(f1s[:, :, f1s_t:f1s_t+s_h, f1s_l:f1s_l+s_w], pad_dim))
        res2.append(pad_to(f2s[:, :, f2s_t:f2s_t+s_h, f2s_l:f2s_l+s_w], pad_dim))
    return torch.cat(res1, dim=0), torch.cat(res2, dim=0)


# Follows the general template of BYOL dataset, with the following changes:
# 1. Flip() is not applied.
# 2. Instead of RandomResizedCrop, a custom Transform, RandomSharedRegionCrop is used.
# 3. The dataset injects two integer tensors alongside the augmentations, which are used to index image regions shared
#    by the joint augmentations.
# 4. The dataset injects an aug_shared_view for debugging purposes.
class StructuredCropDatasetWrapper(Dataset):
    def __init__(self, opt):
        super().__init__()
        self.wrapped_dataset = create_dataset(opt['dataset'])
        augmentations = [RandomApply(augs.ColorJitter(0.8, 0.8, 0.8, 0.2), p=0.8),
            augs.RandomGrayscale(p=0.2),
            RandomApply(filters.GaussianBlur2d((3, 3), (1.5, 1.5)), p=0.1)]
        self.aug = nn.Sequential(*augmentations)
        self.rrc = RandomSharedRegionCrop(opt['latent_multiple'], opt_get(opt, ['jitter_range'], 0))

    def __getitem__(self, item):
        item = self.wrapped_dataset[item]
        a1 = self.aug(item['hq']).squeeze(dim=0)
        a2 = self.aug(item['lq']).squeeze(dim=0)
        a1, a2, sr_dim, m1, m2, db = self.rrc(a1, a2)
        item.update({'aug1': a1, 'aug2': a2, 'similar_region_dimensions': sr_dim,
                     'masked1': m1, 'masked2': m2, 'aug_shared_view': db})
        return item

    def __len__(self):
        return len(self.wrapped_dataset)


# For testing this dataset.
if __name__ == '__main__':
    opt = {
        'dataset':
            {
            'mode': 'imagefolder',
            'name': 'amalgam',
            'paths': ['F:\\4k6k\\datasets\\ns_images\\512_unsupervised'],
            'weights': [1],
            'target_size': 256,
            'force_multiple': 32,
            'scale': 1,
            'fixed_corruptions': ['jpeg-broad', 'gaussian_blur'],
            'random_corruptions': ['noise-5', 'none'],
            'num_corrupts_per_image': 1,
            'corrupt_before_downsize': True,
            },
        'latent_multiple': 8,
        'jitter_range': 0,
    }

    ds = StructuredCropDatasetWrapper(opt)
    import os
    os.makedirs("debug", exist_ok=True)
    for i in tqdm(range(0, len(ds))):
        o = ds[random.randint(0, len(ds)-1)]
        #for k, v in o.items():
            # 'lq', 'hq', 'aug1', 'aug2',
            #if k in [ 'aug_shared_view', 'masked1', 'masked2']:
                #torchvision.utils.save_image(v.unsqueeze(0), "debug/%i_%s.png" % (i, k))
        rcpkg = o['similar_region_dimensions']
        pixun = PixelUnshuffle(8)
        pixsh = nn.PixelShuffle(8)
        rc1, rc2 = reconstructed_shared_regions(pixun(o['aug1'].unsqueeze(0)), pixun(o['aug2'].unsqueeze(0)), rcpkg)
        #torchvision.utils.save_image(pixsh(rc1), "debug/%i_rc1.png" % (i,))
        #torchvision.utils.save_image(pixsh(rc2), "debug/%i_rc2.png" % (i,))
