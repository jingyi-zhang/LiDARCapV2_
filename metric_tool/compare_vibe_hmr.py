import math

import modules.smpl
from modules.geometry import rotation_matrix_to_axis_angle
from scipy.spatial.transform import Rotation as R
from utils import path_util, get_input
import joblib
import json
import shutil
import numpy as np
from plyfile import PlyData
from plyfile import PlyElement
import os
import pickle
# import metric
import torch
from modules.smpl import SMPL
from utils import get_input


def save_image(out_file, image_name, _):
    image = get_input.read_image(image_name)
    get_input.write_image(out_file, image)


def save_crop_image(out_file, image_name, bbox):
    image = get_input.read_image(image_name)
    image_crop = get_input.crop_image(image, bbox)
    get_input.write_image(out_file, image_crop)


def save_mesh(out_file, vertices):
    if type(vertices) == torch.Tensor:
        vertices = vertices.squeeze().cpu().numpy()
    model_file = 'lidarcap/data/basicModel_neutral_lbs_10_207_0_v1.0.0.pkl'
    with open(model_file, 'rb') as f:
        smpl_model = pickle.load(f, encoding='iso-8859-1')
        face_index = smpl_model['f'].astype(np.int64)
    face_1 = np.ones((face_index.shape[0], 1))
    face_1 *= 3
    face = np.hstack((face_1, face_index)).astype(int)
    if os.path.exists(out_file):
        os.remove(out_file)
    with open(out_file, "wb") as zjy_f:
        np.savetxt(zjy_f, vertices, fmt='%f %f %f')
        np.savetxt(zjy_f, face, fmt='%d %d %d %d')
    ply_header = '''ply
format ascii 1.0
element vertex 6890
property float x
property float y
property float z
element face 13776
property list uchar int vertex_indices
end_header
    '''
    with open(out_file, 'r+') as f:
        old = f.read()
        f.seek(0)
        f.write(ply_header)
        f.write(old)


def save_ply(filename, points):
    points = [(points[i, 0], points[i, 1], points[i, 2])
              for i in range(points.shape[0])]
    vertex = np.array(points, dtype=[('x', 'f4'), ('y', 'f4'), ('z', 'f4')])
    el = PlyElement.describe(vertex, 'vertex', comments=['vertices'])
    PlyData([el], text=False).write(filename)


def compute_accel(joints):
    """
    Computes acceleration of 3D joints.
    Args:
        joints (Nx25x3).
    Returns:
        Accelerations (N-2).
    """
    velocities = joints[1:] - joints[:-1]
    acceleration = velocities[1:] - velocities[:-1]
    acceleration_normed = np.linalg.norm(acceleration, axis=2)
    return np.mean(acceleration_normed, axis=1)


def compute_error_accel(joints_gt, joints_pred, vis=None):
    """
    Computes acceleration error:
        1/(n-2) \sum_{i=1}^{n-1} X_{i-1} - 2X_i + X_{i+1}
    Note that for each frame that is not visible, three entries in the
    acceleration error should be zero'd out.
    Args:
        joints_gt (Nx14x3).
        joints_pred (Nx14x3).
        vis (N).
    Returns:
        error_accel (N-2).
    """
    # (N-2)x14x3
    accel_gt = joints_gt[:-2] - 2 * joints_gt[1:-1] + joints_gt[2:]
    accel_pred = joints_pred[:-2] - 2 * joints_pred[1:-1] + joints_pred[2:]

    normed = np.linalg.norm(accel_pred - accel_gt, axis=2)

    if vis is None:
        new_vis = np.ones(len(normed), dtype=bool)
    else:
        invis = np.logical_not(vis)
        invis1 = np.roll(invis, -1)
        invis2 = np.roll(invis, -2)
        new_invis = np.logical_or(invis, np.logical_or(invis1, invis2))[:-2]
        new_vis = np.logical_not(new_invis)

    return np.mean(normed[new_vis], axis=1)


def compute_similarity_transform(S1, S2):
    '''
    Computes a similarity transform (sR, t) that takes
    a set of 3D points S1 (3 x N) closest to a set of 3D points S2,
    where R is an 3x3 rotation matrix, t 3x1 translation, s scale.
    i.e. solves the orthogonal Procrutes problem.
    '''
    transposed = False
    if S1.shape[0] != 3 and S1.shape[0] != 2:
        S1 = S1.T
        S2 = S2.T
        transposed = True
    assert(S2.shape[1] == S1.shape[1])

    # 1. Remove mean.
    mu1 = S1.mean(axis=1, keepdims=True)
    mu2 = S2.mean(axis=1, keepdims=True)
    X1 = S1 - mu1
    X2 = S2 - mu2

    # 2. Compute variance of X1 used for scale.
    var1 = np.sum(X1**2)

    # 3. The outer product of X1 and X2.
    K = X1.dot(X2.T)

    # 4. Solution that Maximizes trace(R'K) is R=U*V', where U, V are
    # singular vectors of K.
    U, s, Vh = np.linalg.svd(K)
    V = Vh.T
    # Construct Z that fixes the orientation of R to get det(R)=1.
    Z = np.eye(U.shape[0])
    Z[-1, -1] *= np.sign(np.linalg.det(U.dot(V.T)))
    # Construct R.
    R = V.dot(Z.dot(U.T))

    # 5. Recover scale.
    scale = np.trace(R.dot(K)) / var1

    # 6. Recover translation.
    t = mu2 - scale * (R.dot(mu1))

    # 7. Error:
    S1_hat = scale * R.dot(S1) + t

    if transposed:
        S1_hat = S1_hat.T

    return S1_hat


def compute_similarity_transform_torch(S1, S2):
    '''
    Computes a similarity transform (sR, t) that takes
    a set of 3D points S1 (3 x N) closest to a set of 3D points S2,
    where R is an 3x3 rotation matrix, t 3x1 translation, s scale.
    i.e. solves the orthogonal Procrutes problem.
    '''
    transposed = False
    if S1.shape[0] != 3 and S1.shape[0] != 2:
        S1 = S1.T
        S2 = S2.T
        transposed = True
    assert (S2.shape[1] == S1.shape[1])

    # 1. Remove mean.
    mu1 = S1.mean(axis=1, keepdims=True)
    mu2 = S2.mean(axis=1, keepdims=True)
    X1 = S1 - mu1
    X2 = S2 - mu2

    # print('X1', X1.shape)

    # 2. Compute variance of X1 used for scale.
    var1 = torch.sum(X1 ** 2)

    # print('var', var1.shape)

    # 3. The outer product of X1 and X2.
    K = X1.mm(X2.T)

    # 4. Solution that Maximizes trace(R'K) is R=U*V', where U, V are
    # singular vectors of K.
    U, s, V = torch.svd(K)
    # V = Vh.T
    # Construct Z that fixes the orientation of R to get det(R)=1.
    Z = torch.eye(U.shape[0], device=S1.device)
    Z[-1, -1] *= torch.sign(torch.det(U @ V.T))
    # Construct R.
    R = V.mm(Z.mm(U.T))

    # print('R', X1.shape)

    # 5. Recover scale.
    scale = torch.trace(R.mm(K)) / var1
    # print(R.shape, mu1.shape)
    # 6. Recover translation.
    t = mu2 - scale * (R.mm(mu1))
    # print(t.shape)

    # 7. Error:
    S1_hat = scale * R.mm(S1) + t

    if transposed:
        S1_hat = S1_hat.T

    return S1_hat


def batch_compute_similarity_transform_torch(S1, S2):
    '''
    Computes a similarity transform (sR, t) that takes
    a set of 3D points S1 (3 x N) closest to a set of 3D points S2,
    where R is an 3x3 rotation matrix, t 3x1 translation, s scale.
    i.e. solves the orthogonal Procrutes problem.
    '''
    transposed = False
    if S1.shape[0] != 3 and S1.shape[0] != 2:
        S1 = S1.permute(0, 2, 1)
        S2 = S2.permute(0, 2, 1)
        transposed = True
    assert(S2.shape[1] == S1.shape[1])

    # 1. Remove mean.
    mu1 = S1.mean(axis=-1, keepdims=True)
    mu2 = S2.mean(axis=-1, keepdims=True)

    X1 = S1 - mu1
    X2 = S2 - mu2

    # 2. Compute variance of X1 used for scale.
    var1 = torch.sum(X1**2, dim=1).sum(dim=1)

    # 3. The outer product of X1 and X2.
    K = X1.bmm(X2.permute(0, 2, 1))

    # 4. Solution that Maximizes trace(R'K) is R=U*V', where U, V are
    # singular vectors of K.
    U, s, V = torch.svd(K)

    # Construct Z that fixes the orientation of R to get det(R)=1.
    Z = torch.eye(U.shape[1], device=S1.device).unsqueeze(0)
    Z = Z.repeat(U.shape[0], 1, 1)
    Z[:, -1, -1] *= torch.sign(torch.det(U.bmm(V.permute(0, 2, 1))))

    # Construct R.
    R = V.bmm(Z.bmm(U.permute(0, 2, 1)))

    # 5. Recover scale.
    scale = torch.cat([torch.trace(x).unsqueeze(0) for x in R.bmm(K)]) / var1

    # 6. Recover translation.
    t = mu2 - (scale.unsqueeze(-1).unsqueeze(-1) * (R.bmm(mu1)))

    # 7. Error:
    S1_hat = scale.unsqueeze(-1).unsqueeze(-1) * R.bmm(S1) + t

    if transposed:
        S1_hat = S1_hat.permute(0, 2, 1)

    return S1_hat


def align_by_pelvis(joints):
    """
    Assumes joints is 14 x 3 in LSP order.
    Then hips are: [3, 2]
    Takes mid point of these points, then subtracts it.
    """

    pelvis = joints[0, :]
    return joints - np.expand_dims(pelvis, axis=0)


def compute_errors(gt3ds, preds):
    """
    Gets MPJPE after pelvis alignment + MPJPE after Procrustes.
    Evaluates on the 24 common joints.
    Inputs:
      - gt3ds: N x 24 x 3
      - preds: N x 24 x 3
    """
    errors, errors_pa = [], []
    errors = np.sqrt(((preds - gt3ds) ** 2).sum(-1)).mean(-1)
    S1_hat = batch_compute_similarity_transform_torch(
        torch.from_numpy(preds).float(), torch.from_numpy(gt3ds).float()).numpy()
    errors_pa = np.sqrt(((S1_hat - gt3ds) ** 2).sum(-1)).mean(-1)
    return errors, errors_pa


def compute_error_verts(pred_verts, target_verts):
    """
    Computes MPJPE over 6890 surface vertices.
    Args:
        verts_gt (Nx6890x3).
        verts_pred (Nx6890x3).
    Returns:
        error_verts (N).
    """

    assert len(pred_verts) == len(target_verts)
    error_per_vert = np.sqrt(np.sum((target_verts - pred_verts) ** 2, axis=2))
    return np.mean(error_per_vert, axis=1)


torsal_length = 0.5127067


def compute_pck(pred_joints, gt_joints, threshold):
    # (B, N, 3), (B, N, 3)
    B = len(pred_joints)
    pred_joints -= pred_joints[:, :1, :]
    gt_joints -= gt_joints[:, :1, :]

    distance = np.sqrt(((pred_joints - gt_joints) ** 2).sum(-1))  # (B, N)
    correct = distance < threshold * torsal_length
    correct = (correct.sum(0) / B).mean()
    return correct

from tqdm import tqdm

def output_metric(pred_poses, gt_poses, batch_size=32):
    assert len(pred_poses) == len(gt_poses)
    n = len(pred_poses)

    smpl = SMPL().cuda()

    #pred_vertices = np.zeros((0, 6890, 3))
    #gt_vertices = np.zeros((0, 6890, 3))
    pred_joints = np.zeros((0, 24, 3))
    gt_joints = np.zeros((0, 24, 3))

    n_batch = (n + batch_size - 1) // batch_size

    #for i in tqdm(range(n_batch)):
    for i in range(n_batch):
        lb = i * batch_size
        ub = (i + 1) * batch_size

        cur_n = min(ub - lb, n - lb)

        cur_pred_vertices = smpl(torch.from_numpy(
            pred_poses[lb:ub]).cuda(), torch.zeros((cur_n, 10)).cuda())
        cur_gt_vertices = smpl(torch.from_numpy(
            gt_poses[lb:ub]).cuda(), torch.zeros((cur_n, 10)).cuda())
        cur_pred_joints = smpl.get_full_joints(cur_pred_vertices)
        cur_gt_joints = smpl.get_full_joints(cur_gt_vertices)

        #pred_vertices = np.concatenate((pred_vertices, cur_pred_vertices.cpu().numpy()))
        #gt_vertices = np.concatenate((gt_vertices, cur_gt_vertices.cpu().numpy()))
        pred_joints = np.concatenate(
            (pred_joints, cur_pred_joints.cpu().numpy()))
        gt_joints = np.concatenate((gt_joints, cur_gt_joints.cpu().numpy()))

    m2mm = 1000

    pred_joints -= pred_joints[:, :1, :]
    gt_joints -= gt_joints[:, :1, :]

    accel_error = np.mean(compute_error_accel(gt_joints, pred_joints)) * m2mm
    mpjpe, pa_mpjpe = compute_errors(gt_joints, pred_joints)
    pjpe = mpjpe
    mpjpe = np.mean(mpjpe) * m2mm
    pa_mpjpe = np.mean(pa_mpjpe) * m2mm
    #pve = np.mean(compute_error_verts(pred_vertices, gt_vertices)) * m2mm
    pck_30 = compute_pck(pred_joints, gt_joints, 0.3)
    pck_50 = compute_pck(pred_joints, gt_joints, 0.5)

    #return pjpe, gt_vertices, pred_vertices
    return mpjpe, pa_mpjpe, pck_30, pck_50, accel_error

    # print(accel_error)
    # print(mpjpe)
    # print(pa_mpjpe)
    # print(pve)
    # print(pck_30)
    # print(pck_50)
    # print()

def mqh_output_metric(pred_joints, gt_joints):
    assert pred_joints.shape[1] == 24 and pred_joints.shape[2] == 3

    m2mm = 1000

    pred_joints -= pred_joints[:, :1, :]
    gt_joints -= gt_joints[:, :1, :]

    accel_error = np.mean(compute_error_accel(gt_joints, pred_joints)) * m2mm
    mpjpe, pa_mpjpe = compute_errors(gt_joints, pred_joints)

    mpjpe = np.mean(mpjpe) * m2mm
    pa_mpjpe = np.mean(pa_mpjpe) * m2mm

    pck_30 = compute_pck(pred_joints, gt_joints, 0.3)
    pck_50 = compute_pck(pred_joints, gt_joints, 0.5)

    return {'mpjpe': mpjpe, 'pa_mpjpe': pa_mpjpe, 'pck_30': pck_30, 'pck_50': pck_50, 'accel_error': accel_error}

if __name__ == "__main__":

    idxs = [0 for i in range(42)]
    idxs[7] = [138]
    idxs[24] = [1188]
    idxs[29] = [2190]
    idxs[41] = []

    pred_pose_name = 'pointnet2_stgcn'

    for id in [41]:
        # we need five file:
        # origin_image, vibe_image, HMR_image, origin_pointcloud, pred_pose
        pointcloud_filenames = get_input.get_sampled_pointcloud(id)
        gt_pose = get_input.get_gt_poses(id)
        origin_images_filenames = get_input.get_images(id)
        vibe_images_filenames = get_input.get_vibe_images(id)
        hmr_images_filenames = get_input.get_hmr_images(id)

        # pred_pose = get_input.get_pred_poses(pred_pose_name, id)
        # pred_pose = pred_pose[:len(gt_pose)].astype(np.float32)

        # pjpe, gt_vertices, pred_vertices = output_metric(pred_pose, gt_pose)
        save_path = 'your_data_path'

        os.makedirs(save_path, exist_ok=True)

        for index in idxs[id]:
            print(id, ' ####', idxs[id])
            file_path = save_path + str(index)
            # file path and name
            origin_image_name = file_path + '_image_origin.png'
            vibe_image_name = file_path + '_image_vibe.png'
            hmr_image_name = file_path + '_image_hmr.png'
            origin_pointcloud_name = file_path + '_pointcloud.ply'
            pred_pose_mesh_name = file_path + '_mesh.ply'

            cloud = get_input.read_point_cloud(pointcloud_filenames[index])
            bbox = get_input.get_bbox(pointcloud_filenames[index])

            # origin_image, vibe_image, HMR_image, origin_pointcloud, pred_pose
            save_ply(origin_pointcloud_name, cloud)
            # save_mesh(pred_pose_mesh_name, pred_vertices[index])
            save_crop_image(origin_image_name,
                            origin_images_filenames[index], bbox)
            save_crop_image(vibe_image_name,
                            vibe_images_filenames[index], bbox)
            save_crop_image(hmr_image_name, hmr_images_filenames[index], bbox)
