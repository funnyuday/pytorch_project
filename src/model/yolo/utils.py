import os
import cv2
import pdb
import time
import copy
import math
import torch
import random
import torchvision
import numpy as np
import matplotlib.pyplot as plt

from tqdm import tqdm
from pathlib import Path
from functools import wraps
from terminaltables import AsciiTable

def fn_timer(funciton):
    @wraps(funciton)
    def function_timer(*args, **kwargs):
        t0 = time.time()
        result = funciton(*args, **kwargs)
        t1 = time.time()
        cost = t1 -t0
        print(f'{funciton.__name__} cost {cost:.2f}s')
        return result
    return function_timer

def bbox_iou(box1, box2, xyxy=True):
    if not xyxy:
        b1_x1, b1_x2 = box1[:, 0] - box1[:, 2] / 2, box1[:, 0] + box1[:, 2] / 2
        b1_y1, b1_y2 = box1[:, 1] - box1[:, 3] / 2, box1[:, 1] + box1[:, 3] / 2
        b2_x1, b2_x2 = box2[:, 0] - box2[:, 2] / 2, box2[:, 0] + box2[:, 2] / 2
        b2_y1, b2_y2 = box2[:, 1] - box2[:, 3] / 2, box2[:, 1] + box2[:, 3] / 2
    else:
        b1_x1, b1_y1, b1_x2, b1_y2 = \
            box1[:, 0], box1[:, 1], box1[:, 2], box1[:, 3]
        b2_x1, b2_y1, b2_x2, b2_y2 = \
            box2[:, 0], box2[:, 1], box2[:, 2], box2[:, 3]

    rect_x1 = torch.max(b1_x1, b2_x1)
    rect_y1 = torch.max(b1_y1, b2_y1)
    rect_x2 = torch.min(b1_x2, b2_x2)
    rect_y2 = torch.min(b1_y2, b2_y2)
    area = torch.clamp(rect_x2 - rect_x1 + 1, min=0) * torch.clamp(rect_y2 - rect_y1 + 1, min=0)
    b1_area = (b1_x2 - b1_x1 + 1) * (b1_y2 - b1_y1 + 1)
    b2_area = (b2_x2 - b2_x1 + 1) * (b2_y2 - b2_y1 + 1)
    iou = area / (b1_area + b2_area - area + 1e-16)
    return iou

def load_classes(path):
    with open(path, 'r') as fp:
        names = fp.read().splitlines()
    return names

def xyxy2xywh(x):
    y = torch.zeros_like(x) if isinstance(x, torch.Tensor) else np.zeros_like(x)
    y[:, 0] = (x[:, 0] + x[:, 2]) / 2
    y[:, 1] = (x[:, 1] + x[:, 3]) / 2
    y[:, 2] = x[:, 2] - x[:, 0]
    y[:, 3] = x[:, 3] - x[:, 1]
    return y

def xywh2xyxy(x):
    y = torch.zeros_like(x) if isinstance(x, torch.Tensor) else np.zeros_like(x)
    y[:, 0] = x[:, 0] - x[:, 2] / 2
    y[:, 1] = x[:, 1] - x[:, 3] / 2
    y[:, 2] = x[:, 0] + x[:, 2] / 2
    y[:, 3] = x[:, 1] + x[:, 3] / 2
    return y

def xywhn2xyxy(x, w=640, h=640, padw=32, padh=32):
    y = x.clone() if isinstance(x, torch.Tensor) else np.copy(x)
    y[:, 0] = w * (x[:, 0] - x[:, 2] / 2) + padw
    y[:, 1] = h * (x[:, 1] - x[:, 3] / 2) + padh
    y[:, 2] = w * (x[:, 0] + x[:, 2] / 2) + padw
    y[:, 3] = h * (x[:, 1] + x[:, 3] / 2) + padh
    return y

def non_max_suppression(prediction, conf_thr=0.25, iou_thr=0.45, classes=None):
    """Performs Non-Maximum Suppression (NMS) on inference results
    Returns:
         detections with shape: nx6 (x1, y1, x2, y2, conf, cls)
    """
    nc = prediction.shape[2] - 5  # number of classes
    # Settings
    # (pixels) minimum and maximum box width and height
    max_wh = 4096
    max_det = 300  # maximum number of detections per image
    max_nms = 30000  # maximum number of boxes into torchvision.ops.nms()
    time_limit = 1.0  # seconds to quit after
    multi_label = nc > 1  # multiple labels per box (adds 0.5ms/img)

    t = time.time()
    output = [torch.zeros((0, 6), device="cpu")] * prediction.shape[0]

    for xi, x in enumerate(prediction):  # image index, image inference
        # Apply constraints
        # x[((x[..., 2:4] < min_wh) | (x[..., 2:4] > max_wh)).any(1), 4] = 0  # width-height
        x = x[x[..., 4] > conf_thr]  # confidence

        # If none remain process next image
        if not x.shape[0]:
            continue

        # Compute conf
        x[:, 5:] *= x[:, 4:5]  # conf = obj_conf * cls_conf

        # Box (center x, center y, width, height) to (x1, y1, x2, y2)
        box = xywh2xyxy(x[:, :4])

        # Detections matrix nx6 (xyxy, conf, cls)
        if multi_label:
            i, j = (x[:, 5:] > conf_thr).nonzero(as_tuple=False).T
            x = torch.cat((box[i], x[i, j + 5, None], j[:, None].float()), 1)
        else:  # best class only
            conf, j = x[:, 5:].max(1, keepdim=True)
            x = torch.cat((box, conf, j.float()), 1)[conf.view(-1) > conf_thr]

        # Filter by class
        if classes is not None:
            x = x[(x[:, 5:6] == torch.tensor(classes, device=x.device)).any(1)]

        # Check shape
        n = x.shape[0]  # number of boxes
        if not n:  # no boxes
            continue
        elif n > max_nms:  # excess boxes
            # sort by confidence
            x = x[x[:, 4].argsort(descending=True)[:max_nms]]

        # Batched NMS
        c = x[:, 5:6] * max_wh  # classes
        # boxes (offset by class), scores
        boxes, scores = x[:, :4] + c, x[:, 4]
        i = torchvision.ops.nms(boxes, scores, iou_thr)  # NMS
        if i.shape[0] > max_det:  # limit detections
            i = i[:max_det]

        output[xi] = x[i]

        if (time.time() - t) > time_limit:
            print(f'WARNING: NMS time limit {time_limit}s exceeded')
            break  # time limit exceeded

    return output

def ap_per_class(tp, conf, pred_cls, target_cls):
    """ Compute the average precision, given the recall and precision curves.
    Source: https://github.com/rafaelpadilla/Object-Detection-Metrics.
    # Arguments
        tp:    True positives (list).
        conf:  Objectness value from 0-1 (list).
        pred_cls: Predicted object classes (list).
        target_cls: True object classes (list).
    # Returns
        The average precision as computed in py-faster-rcnn.
    """

    # Sort by objectness
    i = np.argsort(-conf)
    tp, conf, pred_cls = tp[i], conf[i], pred_cls[i]

    # Find unique classes
    unique_classes = np.unique(target_cls)

    # Create Precision-Recall curve and compute AP for each class
    ap, p, r, mr = [], [], [], []
    for c in tqdm(unique_classes, desc="Computing AP"):
        i = pred_cls == c
        n_gt = (target_cls == c).sum()  # Number of ground truth objects
        n_p = i.sum()  # Number of predicted objects

        if n_p == 0 and n_gt == 0:
            continue
        elif n_p == 0 or n_gt == 0:
            ap.append(0)
            r.append(0)
            p.append(0)
            mr.append(0)
        else:
            # Accumulate FPs and TPs
            fpc = (1 - tp[i]).cumsum()
            tpc = (tp[i]).cumsum()

            # Recall
            recall_curve = tpc / (n_gt + 1e-16)
            r.append(recall_curve[-1])

            # Precision
            precision_curve = tpc / (tpc + fpc)
            p.append(precision_curve[-1])

            # AP from recall-precision curve
            ap.append(compute_ap(recall_curve, precision_curve))

            # log average miss rate
            mr.append(log_average_miss_rate(recall_curve, precision_curve))

    # Compute F1 score (harmonic mean of precision and recall)
    p, r, ap, mr = np.array(p), np.array(r), np.array(ap), np.array(mr)
    f1 = 2 * p * r / (p + r + 1e-16)

    return p, r, ap, f1, mr, unique_classes.astype("int32")

def compute_ap(recall, precision):
    """ Compute the average precision, given the recall and precision curves.
    Code originally from https://github.com/rbgirshick/py-faster-rcnn.

    # Arguments
        recall:    The recall curve (list).
        precision: The precision curve (list).
    # Returns
        The average precision as computed in py-faster-rcnn.
    """
    # correct AP calculation
    # first append sentinel values at the end
    mrec = np.concatenate(([0.0], recall, [1.0]))
    mpre = np.concatenate(([0.0], precision, [0.0]))

    # compute the precision envelope
    for i in range(mpre.size - 1, 0, -1):
        mpre[i - 1] = np.maximum(mpre[i - 1], mpre[i])

    # to calculate area under PR curve, look for points
    # where X axis (recall) changes value
    i = np.where(mrec[1:] != mrec[:-1])[0]

    # and sum (\Delta recall) * prec
    ap = np.sum((mrec[i + 1] - mrec[i]) * mpre[i + 1])
    return ap

def get_batch_statistics(outputs, targets, iou_thr):
    """ Compute true positives, predicted scores and predicted labels per sample """
    batch_metrics = []
    for sample_i in range(len(outputs)):
        if outputs[sample_i] is None:
            continue
        output = outputs[sample_i]
        pred_boxes = output[:, :4]
        pred_scores = output[:, 4]
        pred_labels = output[:, -1]
        true_positives = np.zeros(pred_boxes.shape[0])
        iou_score = np.zeros(pred_boxes.shape[0])

        annotations = targets[targets[:, 0] == sample_i][:, 1:]
        target_labels = annotations[:, 0] if len(annotations) else []
        if len(annotations):
            detected_boxes = []
            target_boxes = annotations[:, 1:]
            for pred_i, (pred_box, pred_label) in enumerate(zip(pred_boxes, pred_labels)):
                # If targets are found break
                if len(detected_boxes) == len(annotations):
                    break
                # Ignore if label is not one of the target labels
                if pred_label not in target_labels:
                    continue
                iou, box_index = bbox_iou(pred_box.unsqueeze(0), target_boxes[annotations[..., 0] == pred_label]).max(0)
                res_box = target_boxes[annotations[..., 0] == pred_label][box_index].tolist()

                iou_score[pred_i] = iou
                if iou >= iou_thr and res_box not in detected_boxes:
                    true_positives[pred_i] = 1
                    detected_boxes += [res_box]
        batch_metrics.append([true_positives, pred_scores, pred_labels, iou_score])
    return batch_metrics

def log_average_miss_rate(prec, rec):
    if prec.size == 0:
        lamr = 0
        mr = 1
        fppi = 0
        return lamr, mr, fppi
    fppi = (1 - prec)
    mr = (1 - rec)
    fppi_tmp = np.insert(fppi, 0, -1.0)
    mr_tmp = np.insert(mr, 0, 1.0)

    ref = np.logspace(-2.0, 0.0, num = 9)
    for i, ref_i in enumerate(ref):
        j = np.where(fppi_tmp <= ref_i)[-1][-1]
        ref[i] = mr_tmp[j]

    lamr = math.exp(np.mean(np.log(np.maximum(1e-10, ref))))
    return lamr

def rescale_boxes(boxes, current_dim, original_shape, no_letter=False):
    """
    Rescales bounding boxes to the original shape
    """
    orig_h, orig_w = original_shape

    # The amount of padding that was added
    pad_x = 0 if no_letter else max(orig_h - orig_w, 0) * (current_dim[0] / max(original_shape))
    pad_y = 0 if no_letter else max(orig_w - orig_h, 0) * (current_dim[1] / max(original_shape))

    # Image height and width after padding is removed
    unpad_h = current_dim[1] - pad_y
    unpad_w = current_dim[0] - pad_x

    # Rescale bounding boxes to dimension of original image
    boxes[:, 0] = ((boxes[:, 0] - pad_x // 2) / unpad_w) * orig_w
    boxes[:, 1] = ((boxes[:, 1] - pad_y // 2) / unpad_h) * orig_h
    boxes[:, 2] = ((boxes[:, 2] - pad_x // 2) / unpad_w) * orig_w
    boxes[:, 3] = ((boxes[:, 3] - pad_y // 2) / unpad_h) * orig_h
    return boxes

def draw_scatter_diagram(opt, conf, pred_cls, iou_score, class_names):
    colors = ['maroon', 'hotpink', 'brown', 'red', 'purple', 'skyblue', 'silver', 'green', 'orange',
              'cyan']
    area = np.pi * 2**2
    save_dir = Path(opt.save_dir) / 'scatter_diagram'
    save_dir.mkdir(parents=True, exist_ok=True)
    for i in range(len(class_names)):
        mask = pred_cls == 1
        plt.cla()
        plt.xlabel('IoU with ground-truth box')
        plt.ylabel('Localization Confidence')
        plt.xlim(xmax=1.0, xmin=0)
        plt.ylim(ymax=1.0, ymin=0)
        plt.scatter(iou_score[mask], conf[mask], s=area, c=colors[i], alpha=0.4, label=class_names[i])
        plt.plot([0, 1], [0, 1], c='b', ls='--')
        plt.legend(fontsize=8)
        plt.savefig(save_dir / f'{class_names[i]} IoU vc Conf.jpg', dpi=300)
    plt.close()

def draw_and_save_output_images(img_detections, imgs, opt, classes):
    colors = {name:(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) for name in classes}
    for i, (image_path, detections) in tqdm(enumerate(zip(imgs, img_detections)), total=len(imgs), desc="Draw result"):
        _draw_and_save_output_image(image_path, detections, opt, classes, colors)

def _draw_and_save_output_image(image_path, detections, opt, classes, colors):
    assert opt.data_root in image_path
    output_path = Path(opt.save_dir) / "draw_result" / os.path.dirname(image_path).replace(opt.data_root, '')
    output_path.mkdir(parents=True, exist_ok=True)
    img = cv2.imread(image_path)
    detections = copy.deepcopy(detections)
    detections = rescale_boxes(detections, opt.img_size, img.shape[:2], opt.no_letter)

    for i, res in enumerate(detections):
        x1, y1, x2, y2, conf, cls_pred = res
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        name_color = colors[classes[int(cls_pred)]]
        img = cv2.rectangle(img, (x1, y1), (x2, y2), name_color, 2)
        text = "{}:{:.1f}%".format(classes[int(cls_pred)], conf*100)
        txt_color = (0, 0, 0) if np.mean(name_color) > 0.5 else (255, 255, 255)
        font = cv2.FONT_HERSHEY_SIMPLEX
        txt_size = cv2.getTextSize(text, font, 0.4, 1)[0]
        txt_bk_color = name_color
        cv2.rectangle(img, (x1, y1 + 1), (x1 + txt_size[0] + 1, y1 + int(1.5*txt_size[1])), txt_bk_color, -1)
        cv2.putText(img, text, (x1, y1 + txt_size[1]), font, 0.4, txt_color, thickness=1)
    filename = os.path.basename(image_path).split(".")[0]
    output_path = os.path.join(str(output_path), f"{filename}.jpg")
    cv2.imwrite(output_path, img)

def print_eval_stats(metrics_output, class_names):
    if metrics_output is not None:
        p, r, ap, f1, mr, ap_cls = metrics_output
        ap_tab = [["Class", "Precision", "Recall", "AP", "F1-score", "log-MR"]]
        for i, c in enumerate(ap_cls):
            ap_tab += [[class_names[c], "%.2f" % p[i], "%.2f" % r[i], "%.2f" % ap[i], "%.2f" % f1[i], "%.2f" % mr[i]]]
        print(AsciiTable(ap_tab).table)
        print(f"---- mAP {ap.mean() :.2f} ----")
    else:
        print("---- mAP not measured (no detections found by model) ----")

def letter_box(size, img):
    w_o, h_o = img.shape[1], img.shape[0]
    w, h = size
    sized = np.full((w, h, 3), 127.5)
    new_ar = w_o / h_o
    if new_ar < 1:
        nh = h
        nw = nh * new_ar
    else:
        nw = w
        nh = nw / new_ar
    resized = cv2.resize(img, (int(nw), int(nh)), interpolation=cv2.INTER_CUBIC)
    lx = int((w - nw) // 2)
    ly = int((h - nh) // 2)
    rx = int(nw) + lx
    ry = int(nh) + ly
    sized[ly:ry, lx:rx, :] = resized
    return sized

def xyxy2darknet(x1, y1, x2, y2, c, shape):
    ori_h, ori_w = shape
    x = str((x1 + x2) / (2 * ori_w))
    y = str((y1 + y2) / (2 * ori_h))
    w = str((x2 - x1) / ori_w)
    h = str((y2 - y1) / ori_h)
    return " ".join([c, x, y, w, h]) + "\n"