import os
import logging
from .base_video_dataset import BaseVideoDataset
from trackron.utils.misc import jpeg4py_loader
import torch
import random
from pycocotools.coco import COCO
from collections import OrderedDict


class MSCOCOSeq(BaseVideoDataset):
    """ The COCO dataset. COCO is an image dataset. Thus, we treat each image as a sequence of length 1.

    Publication:
        Microsoft COCO: Common Objects in Context.
        Tsung-Yi Lin, Michael Maire, Serge J. Belongie, Lubomir D. Bourdev, Ross B. Girshick, James Hays, Pietro Perona,
        Deva Ramanan, Piotr Dollar and C. Lawrence Zitnick
        ECCV, 2014
        https://arxiv.org/pdf/1405.0312.pdf

    Download the images along with annotations from http://cocodataset.org/#download. The root folder should be
    organized as follows.
        - coco_root
            - annotations
                - instances_train2014.json
                - instances_train2017.json
            - train2014
            - train2017

    Note: You also have to install the coco pythonAPI from https://github.com/cocodataset/cocoapi.
    """

    def __init__(self,
                 root,
                 image_loader=jpeg4py_loader,
                 data_fraction=None,
                 split="train",
                 version="2014"):
        """
        args:
            root - path to the coco dataset.
            image_loader (default_image_loader) -  The function to read the images. If installed,
                                                   jpeg4py (https://github.com/ajkxyz/jpeg4py) is used by default. Else,
                                                   opencv's imread is used.
            data_fraction (None) - Fraction of images to be used. The images are selected randomly. If None, all the
                                  images  will be used
            split - 'train' or 'val'.
            version - version of coco dataset (2014 or 2017)
        """
        super().__init__('COCO', root, image_loader)

        # TRACED coco 路径的问题！！！系统中没有将图片单独创建一个文件夹
        # self.img_pth = os.path.join(root, 'images/{}{}/'.format(split, version))
        self.img_pth = os.path.join(root, '{}{}/'.format(split, version))
        self.anno_path = os.path.join(
            root, 'annotations/instances_{}{}.json'.format(split, version))

        # Load the COCO set.
        self.coco_set = COCO(self.anno_path)
        for idx, k in enumerate(self.coco_set.anns):
            self.coco_set.anns[k]['track_id'] = idx

        self.cats = self.coco_set.cats

        self.class_list = self.get_class_list()

        self.sequence_list = self._get_sequence_list()

        if data_fraction is not None:
            self.sequence_list = random.sample(
                self.sequence_list,
                int(len(self.sequence_list) * data_fraction))
        self.seq_per_class = self._build_seq_per_class()

    def _get_sequence_list(self):
        ann_list = list(self.coco_set.anns.keys())
        seq_list = [
            a for a in ann_list if self.coco_set.anns[a]['iscrowd'] == 0
        ]

        return seq_list

    def is_video_sequence(self):
        return False

    def get_num_classes(self):
        return len(self.class_list)

    def get_name(self):
        return 'coco'

    def has_class_info(self):
        return True

    def get_class_list(self):
        class_list = []
        for cat_id in self.cats.keys():
            class_list.append(self.cats[cat_id]['name'])
        return class_list

    def has_segmentation_info(self):
        return True

    def get_num_sequences(self):
        return len(self.sequence_list)

    def _build_seq_per_class(self):
        seq_per_class = {}
        for i, seq in enumerate(self.sequence_list):
            class_name = self.cats[self.coco_set.anns[seq]
                                   ['category_id']]['name']
            if class_name not in seq_per_class:
                seq_per_class[class_name] = [i]
            else:
                seq_per_class[class_name].append(i)

        return seq_per_class

    def get_sequences_in_class(self, class_name):
        return self.seq_per_class[class_name]

    def _get_sequence_resolution(self, anno):
        image_info = self.coco_set.loadImgs(anno['image_id'])
        height = image_info[0]['height']
        width = image_info[0]['width']
        return (height, width)

    def get_sequence_info(self, seq_id):
        anno = self._get_anno(seq_id)
        height, width = self._get_sequence_resolution(anno)

        bbox = torch.Tensor(anno['bbox']).view(1, 4)

        mask = torch.Tensor(self.coco_set.annToMask(anno)).unsqueeze(dim=0)

        # valid = (bbox[:, 2] > 0) & (bbox[:, 3] > 0)
        valid = (bbox[:, 0] >= 0) & (bbox[:, 1] >= 0) & (bbox[:, 2] > 0) & (
            bbox[:, 3] > 0) & (bbox[:, 0] + bbox[:, 2] <= width) & (
                bbox[:, 1] + bbox[:, 3] <= height) & (bbox[:, 2] * bbox[:, 3] >
                                                      1000)
        visible = valid.clone().byte()

        return {'bbox': bbox, 'mask': mask, 'valid': valid, 'visible': visible}

    def _get_anno(self, seq_id):
        anno = self.coco_set.anns[self.sequence_list[seq_id]]

        return anno

    def _get_frames(self, seq_id):
        path = self.coco_set.loadImgs([
            self.coco_set.anns[self.sequence_list[seq_id]]['image_id']
        ])[0]['file_name']
        img = self.image_loader(os.path.join(self.img_pth, path))
        return img

    def get_meta_info(self, seq_id):
        try:
            cat_dict_current = self.cats[self.coco_set.anns[
                self.sequence_list[seq_id]]['category_id']]
            object_meta = OrderedDict({
                'object_class_name':
                cat_dict_current['name'],
                'motion_class':
                None,
                'major_class':
                cat_dict_current['supercategory'],
                'root_class':
                None,
                'motion_adverb':
                None
            })
        except:
            object_meta = OrderedDict({
                'object_class_name': None,
                'motion_class': None,
                'major_class': None,
                'root_class': None,
                'motion_adverb': None
            })
        return object_meta

    def get_class_name(self, seq_id):
        cat_dict_current = self.cats[self.coco_set.anns[
            self.sequence_list[seq_id]]['category_id']]
        return cat_dict_current['name']

    def get_frames(self, seq_id=None, frame_ids=None, anno=None):
        # COCO is an image dataset. Thus we replicate the image denoted by seq_id len(frame_ids) times, and return a
        # list containing these replicated images.
        frame = self._get_frames(seq_id)

        frame_list = [frame.copy() for _ in frame_ids]

        if anno is None:
            anno = self.get_sequence_info(seq_id)

        anno_frames = {}
        for key, value in anno.items():
            anno_frames[key] = [value[0, ...] for _ in frame_ids]

        object_meta = self.get_meta_info(seq_id)

        return frame_list, anno_frames, object_meta

    def get_mot_frames(self, seq_id=None, frame_ids=None, anno=None):
        image_info = self.coco_set.loadImgs(
            [self.coco_set.anns[self.sequence_list[seq_id]]['image_id']])[0]
        path = image_info['file_name']
        frame = self.image_loader(os.path.join(self.img_pth, path))
        frame_list = [frame.copy() for _ in frame_ids]

        # obj_class = self.get_class_name(seq_id)

        if anno is None:
            ann_ids = self.coco_set.getAnnIds(imgIds=[image_info['id']])
            anno = self.coco_set.loadAnns(ann_ids)
            # for idx, k in enumerate(anno):
            #   anno['track_id'] = k
        anno = [
            ann.update({'class_name': self.cats[ann['category_id']]})
            for ann in anno
        ]

        anns = [anno.copy() for _ in frame_ids]
        # anno = self.get_sequence_info(seq_id)

        return frame_list, anns
