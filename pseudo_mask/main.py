#!/usr/bin/env python
# coding: utf-8

from __future__ import print_function

import copy
import os, os.path

import click
import cv2
import matplotlib.cm as cm
import numpy as np
import torch
import torch.nn.functional as F
from torchvision import models, transforms

import torchvision
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torchvision.models as models
import torchvision.transforms as transforms
from torch.utils.data import Dataset  # DataLoader
# from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import SubsetRandomSampler

import gc

from grad_cam import (
    BackPropagation,
    GradCAM,
)

# if a model includes LSTM, such as in image captioning,
# torch.backends.cudnn.enabled = False

class Resnext50(nn.Module):
    def __init__(self, n_classes):
        super().__init__()
        resnet = models.resnext50_32x4d(pretrained=True)
        resnet.fc = nn.Sequential(
            nn.Dropout(p=0.2),
            nn.Linear(in_features=resnet.fc.in_features, out_features=512),
            nn.LeakyReLU(0.1),
            nn.Dropout(p=0.3),
            nn.Linear(512, n_classes)
        )
        self.base_model = resnet
        self.sigm = nn.Sigmoid()

    def forward(self, x):
        return self.sigm(self.base_model(x))

def get_device(cuda):
    cuda = cuda and torch.cuda.is_available()
    device = torch.device("cuda" if cuda else "cpu")
    if cuda:
        current_device = torch.cuda.current_device()
        print("Device:", torch.cuda.get_device_name(current_device))
    else:
        print("Device: CPU")
    return device


# def load_images(image_paths):
#     images = []
#     raw_images = []
#     print("Images:")
#     for i, image_path in enumerate(image_paths):
#         print("\t#{}: {}".format(i, image_path))
#         image, raw_image = preprocess(image_path)
#         images.append(image)
#         raw_images.append(raw_image)
#     return images, raw_images

def load_image(image_path):
    images = []
    raw_images = []
    print("Images:")

    print("\t#{}".format(image_path))
    image, raw_image = preprocess(image_path)
    images.append(image)
    raw_images.append(raw_image)

    return images, raw_images

# def get_classtable():
#     classes = []
#     with open("samples/synset_words.txt") as lines:
#         for line in lines:
#             line = line.strip().split(" ", 1)[1]
#             line = line.split(", ", 1)[0].replace(" ", "_")
#             classes.append(line)
#     return classes

def get_classtable():
    classes = []
    class_dict = {}
    with open("data/classes.txt") as lines:
        for line in lines:
            line = line.strip().split(",", 1)
            classes.append(line[1])
            class_dict[line[1]] = line[0]
    # print(classes)
    return classes

def preprocess(image_path):
    print(image_path)
    raw_image = cv2.imread(image_path)
    raw_image = cv2.resize(raw_image, (224,) * 2)
    image = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ]
    )(raw_image[..., ::-1].copy())
    return image, raw_image


def save_gradient(filename, gradient):
    gradient = gradient.cpu().numpy().transpose(1, 2, 0)
    gradient -= gradient.min()
    gradient /= gradient.max()
    gradient *= 255.0
    cv2.imwrite(filename, np.uint8(gradient))


def save_gradcam(filename, gcam, raw_image, paper_cmap=False):
    gcam = gcam.cpu().numpy()
    cmap = cm.jet_r(gcam)[..., :3] * 255.0
    if paper_cmap:
        alpha = gcam[..., None]
        gcam = alpha * cmap + (1 - alpha) * raw_image
    else:
        gcam = (cmap.astype(np.float) + raw_image.astype(np.float)) / 2
    cv2.imwrite(filename, np.uint8(gcam))

def update_mask(numpy_mask, gcam, img_id):
    # cv2.imshow("1",numpy_mask)
    # cv2.waitKey(0) 
    gcam = gcam.cpu().numpy()
    # cmap = cm.jet_r(gcam)[..., :3] * 255.0
    cmap = gcam * 255.0
    #  Filter those values below arbitrary number of 2/3
    cmap[cmap < 3*255/4] = 0

    

    # PLACEHOLDER class - mod 255
    cmap[cmap >= 3*255/4] = img_id
    mask = (numpy_mask == 0) & (cmap != 0)
    #  Update numpy mask
    numpy_mask[mask] =  img_id

    # cv2.imshow("1",numpy_mask)
    # cv2.waitKey(0) 
    return numpy_mask

    # gcam = gcam.cpu.numpy()
    # cmap = gcam * 255.0
    # #  Filter those values below arbitrary number of 2/3
    # cmap[cmap < 3*255/4] = 0
    # # PLACEHOLDER
    # cmap[cmap >= 3*255/4] = img_id % 255

    # #  Update numpy mask
    # numpy_mask[numpy_mask == 0] = cmap

def save_mask(filename, numpy_mask, img_id):
    numpy_mask = (numpy_mask.astype(np.float))
    cv2.imwrite(filename, np.uint8(numpy_mask))

#  Generate mask from image
# def save_mask(filename, gcam, img_id):
#     gcam = gcam.cpu().numpy()
#     # cmap = cm.jet_r(gcam)[..., :3] * 255.0
#     cmap = gcam * 255.0
#     #  Filter those values below arbitrary number of 2/3
#     cmap[cmap < 3*255/4] = 0

#     # PLACEHOLDER
#     cmap[cmap >= 3*255/4] = img_id % 255
#     gcam = (cmap.astype(np.float))
#     cv2.imwrite(filename, np.uint8(gcam))

def save_sensitivity(filename, maps):
    maps = maps.cpu().numpy()
    scale = max(maps[maps > 0].max(), -maps[maps <= 0].min())
    maps = maps / scale * 0.5
    maps += 0.5
    maps = cm.bwr_r(maps)[..., :3]
    maps = np.uint8(maps * 255.0)
    maps = cv2.resize(maps, (224, 224), interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(filename, maps)

def make_cam(image_paths, target_layer, topk, output_dir, cuda):
    """
    Visualize model responses given multiple images
    """
    device = get_device(cuda)
    image_name = image_paths.strip().split("/")[2]
    image_name = image_name.strip().split(".")[0]

    # print(image_name)

    # Synset words
    classes = get_classtable()

    # Model from torchvision
    model = Resnext50(103) 

    model.to(device)
    model.load_state_dict(torch.load("models/The_10_epoch_ResNext.pkl", map_location=device))

    model.eval()
    # print(model)
    # Images
    image, raw_image = load_image(image_paths)
    image = torch.stack(image).to(device)

    """
    Common usage:
    1. Wrap your model with visualization classes defined in grad_cam.py
    2. Run forward() with images
    3. Run backward() with a list of specific classes
    4. Run generate() to export results
    """

    # =========================================================================
    print("Backpropagation:")

    bp = BackPropagation(model)


    probs, ids = bp.forward(image)  # sorted

    print("Grad-CAM/Guided Backpropagation/Guided Grad-CAM:")

    gcam = GradCAM(model)
    _ = gcam.forward(image)
    #  Create empty mask of size 224 by 224
    numpy_mask = np.zeros((224,224))

    for i in range(topk):

        # Grad-CAM
        gcam.backward(ids[:, [i]])
        # print("ID")
        # print(ids[:, [i]])
        regions = gcam.generate(target_layer)

        # convert tensor to cpu memory then convert to numpy  
        img_id = int(ids[0,i].cpu().numpy())
        # print(img_id)
        print("\t#{}: {} ({:.5f})".format(img_id, classes[ids[0, i]], probs[0, i]))

        # Grad-CAM
        save_gradcam(
            filename=os.path.join(output_dir,"{}-gradcam-{}-{}.png".format(image_name, probs[0, i], classes[ids[0, i]]),
            ),
            gcam=regions[0, 0],
            raw_image=raw_image[0],
        )

        # mask_filename = os.path.join("mask/","{}-mask-{}-{}.png".format(j, target_layer, classes[ids[j, i]]))
        # save_mask(mask_filename,regions[j, 0],img_id)
        numpy_mask = update_mask(numpy_mask, regions[0, 0], img_id)
        
    mask_filename = os.path.join("mask/","{}-mask.png".format(image_name))
    save_mask(mask_filename,numpy_mask,img_id)       

def main():
    data_dir = "samples/train"
    for filename in os.listdir(data_dir):
        if filename.endswith(".jpg"):
            print(filename)
            make_cam(os.path.join(data_dir, filename), "base_model.layer4", 3, "./results", True)


if __name__ == "__main__":
    main()
