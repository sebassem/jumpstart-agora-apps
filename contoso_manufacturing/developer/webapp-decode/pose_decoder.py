import numpy as np
from scipy.optimize import linear_sum_assignment
import cv2

class Pose:
    def __init__(self, num_joints, tag_size=1):
        self.num_joints = num_joints
        self.tag_size = tag_size
        # 2 is for x, y and 1 is for joint confidence
        self.pose = np.zeros((num_joints, 2 + 1 + tag_size), dtype=np.float32)
        self.pose_tag = np.zeros(tag_size, dtype=np.float32)
        self.valid_points_num = 0
        self.c = np.zeros(2, dtype=np.float32)

    def add(self, idx, joint, tag):
        self.pose[idx] = joint
        self.c = self.c * self.valid_points_num + joint[:2]
        self.pose_tag = (self.pose_tag * self.valid_points_num) + tag
        self.valid_points_num += 1
        self.c /= self.valid_points_num
        self.pose_tag /= self.valid_points_num

    @property
    def tag(self):
        if self.valid_points_num > 0:
            return self.pose_tag
        return None

    @property
    def center(self):
        if self.valid_points_num > 0:
            return self.c
        return None

class AssociativeEmbeddingDecoder:
    def __init__(self, num_joints, max_num_people, detection_threshold, use_detection_val,
                 ignore_too_much, tag_threshold, pose_threshold,
                 adjust=True, refine=True, delta=0.0, joints_order=None,
                 dist_reweight=True):
        self.num_joints = num_joints
        self.max_num_people = max_num_people
        self.detection_threshold = detection_threshold
        self.tag_threshold = tag_threshold
        self.pose_threshold = pose_threshold
        self.use_detection_val = use_detection_val
        self.ignore_too_much = ignore_too_much

        if self.num_joints == 17 and joints_order is None:
            self.joint_order = (0, 1, 2, 3, 4, 5, 6, 11, 12, 7, 8, 9, 10, 13, 14, 15, 16)
        else:
            self.joint_order = list(np.arange(self.num_joints))

        self.do_adjust = adjust
        self.do_refine = refine
        self.dist_reweight = dist_reweight
        self.delta = delta

    @staticmethod
    def _max_match(scores):
        r, c = linear_sum_assignment(scores)
        return np.stack((r, c), axis=1)

    def _match_by_tag(self, inp):
        tag_k, loc_k, val_k = inp
        embd_size = tag_k.shape[2]
        all_joints = np.concatenate((loc_k, val_k[..., None], tag_k), -1)

        poses = []
        for idx in self.joint_order:
            tags = tag_k[idx]
            joints = all_joints[idx]
            mask = joints[:, 2] > self.detection_threshold
            tags = tags[mask]
            joints = joints[mask]

            if len(poses) == 0:
                for tag, joint in zip(tags, joints):
                    pose = Pose(self.num_joints, embd_size)
                    pose.add(idx, joint, tag)
                    poses.append(pose)
                continue

            if joints.shape[0] == 0 or (self.ignore_too_much and len(poses) == self.max_num_people):
                continue

            poses_tags = np.stack([p.tag for p in poses], axis=0)
            diff = tags[:, None] - poses_tags[None, :]
            diff_normed = np.linalg.norm(diff, ord=2, axis=2)
            diff_saved = np.copy(diff_normed)

            if self.dist_reweight:
                # Reweight cost matrix to prefer nearby points among all that are close enough in a tag space.
                centers = np.stack([p.center for p in poses], axis=0)[None]
                dists = np.linalg.norm(joints[:, :2][:, None, :] - centers, ord=2, axis=2)
                close_tags_masks = diff_normed < self.tag_threshold
                min_dists = np.min(dists, axis=0, keepdims=True)
                dists /= min_dists + 1e-10
                diff_normed[close_tags_masks] *= dists[close_tags_masks]

            if self.use_detection_val:
                diff_normed = np.round(diff_normed) * 100 - joints[:, 2:3]
            num_added = diff.shape[0]
            num_grouped = diff.shape[1]
            if num_added > num_grouped:
                diff_normed = np.pad(diff_normed, ((0, 0), (0, num_added - num_grouped)),
                                     mode='constant', constant_values=1e10)

            pairs = self._max_match(diff_normed)
            for row, col in pairs:
                if row < num_added and col < num_grouped and diff_saved[row][col] < self.tag_threshold:
                    poses[col].add(idx, joints[row], tags[row])
                else:
                    pose = Pose(self.num_joints, embd_size)
                    pose.add(idx, joints[row], tags[row])
                    poses.append(pose)

        ans = np.asarray([p.pose for p in poses], dtype=np.float32).reshape(-1, self.num_joints, 2 + 1 + embd_size)
        tags = np.asarray([p.tag for p in poses], dtype=np.float32).reshape(-1, embd_size)
        return ans, tags

    def top_k(self, heatmaps, tags):
        N, K, H, W = heatmaps.shape
        heatmaps = heatmaps.reshape(N, K, -1)
        ind = heatmaps.argpartition(-self.max_num_people, axis=2)[:, :, -self.max_num_people:]
        val_k = np.take_along_axis(heatmaps, ind, axis=2)
        subind = np.argsort(-val_k, axis=2)
        ind = np.take_along_axis(ind, subind, axis=2)
        val_k = np.take_along_axis(val_k, subind, axis=2)

        tags = tags.reshape(N, K, W * H, -1)
        tag_k = [np.take_along_axis(tags[..., i], ind, axis=2) for i in range(tags.shape[3])]
        tag_k = np.stack(tag_k, axis=3)

        x = ind % W
        y = ind // W
        loc_k = np.stack((x, y), axis=3)
        return tag_k, loc_k, val_k

    @staticmethod
    def adjust(ans, heatmaps):
        H, W = heatmaps.shape[-2:]
        for batch_idx, people in enumerate(ans):
            for person in people:
                for k, joint in enumerate(person):
                    heatmap = heatmaps[batch_idx, k]
                    px = int(joint[0])
                    py = int(joint[1])
                    if 1 < px < W - 1 and 1 < py < H - 1:
                        diff = np.array([
                            heatmap[py, px + 1] - heatmap[py, px - 1],
                            heatmap[py + 1, px] - heatmap[py - 1, px]
                        ])
                        joint[:2] += np.sign(diff) * .25
        return ans

    @staticmethod
    def refine(heatmap, tag, keypoints, pose_tag=None):
        K, H, W = heatmap.shape
        if len(tag.shape) == 3:
            tag = tag[..., None]

        if pose_tag is not None:
            prev_tag = pose_tag
        else:
            tags = []
            for i in range(K):
                if keypoints[i, 2] > 0:
                    x, y = keypoints[i][:2].astype(int)
                    tags.append(tag[i, y, x])
            prev_tag = np.mean(tags, axis=0)

        for i, (_heatmap, _tag) in enumerate(zip(heatmap, tag)):
            if keypoints[i, 2] > 0:
                continue
            # Get position with the closest tag value to the pose tag.
            diff = np.abs(_tag[..., 0] - prev_tag) + 0.5
            diff = diff.astype(np.int32).astype(_heatmap.dtype)
            diff -= _heatmap
            idx = diff.argmin()
            y, x = np.divmod(idx, _heatmap.shape[-1])
            # Corresponding keypoint detection score.
            val = _heatmap[y, x]
            if val > 0:
                keypoints[i, :3] = x, y, val
                if 1 < x < W - 1 and 1 < y < H - 1:
                    diff = np.array([
                        _heatmap[y, x + 1] - _heatmap[y, x - 1],
                        _heatmap[y + 1, x] - _heatmap[y - 1, x]
                    ])
                    keypoints[i, :2] += np.sign(diff) * .25

        return keypoints

    def __call__(self, heatmaps, tags, nms_heatmaps):
        tag_k, loc_k, val_k = self.top_k(nms_heatmaps, tags)
        ans = tuple(map(self._match_by_tag, zip(tag_k, loc_k, val_k)))  # Call _match_by_tag() for each element in batch
        ans, ans_tags = map(list, zip(*ans))

        np.abs(heatmaps, out=heatmaps)

        if self.do_adjust:
            ans = self.adjust(ans, heatmaps)

        if self.delta != 0.0:
            for people in ans:
                for person in people:
                    for joint in person:
                        joint[:2] += self.delta

        ans = ans[0]
        scores = np.asarray([i[:, 2].mean() for i in ans])
        mask = scores > self.pose_threshold
        ans = ans[mask]
        scores = scores[mask]

        if self.do_refine:
            heatmap_numpy = heatmaps[0]
            tag_numpy = tags[0]
            for i, pose in enumerate(ans):
                ans[i] = self.refine(heatmap_numpy, tag_numpy, pose, ans_tags[0][i])

        return ans, scores

def resize_image(image, size, keep_aspect_ratio=False, interpolation=cv2.INTER_LINEAR):
    if not keep_aspect_ratio:
        resized_frame = cv2.resize(image, size, interpolation=interpolation)
    else:
        h, w = image.shape[:2]
        scale = min(size[1] / h, size[0] / w)
        resized_frame = cv2.resize(image, None, fx=scale, fy=scale, interpolation=interpolation)
    return resized_frame