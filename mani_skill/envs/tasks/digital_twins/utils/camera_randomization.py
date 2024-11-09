"""Create a viewable pointcloud of sampled camera positions, all positons are offset from the robot base position"""
from dataclasses import dataclass

import numpy as np
import sapien
import torch
import tyro

from mani_skill.sensors.camera import CameraConfig
from mani_skill.utils import sapien_utils
from mani_skill.utils.building.actors.common import build_sphere
from mani_skill.utils.geometry.rotation_conversions import (
    axis_angle_to_quaternion,
    euler_angles_to_matrix,
    quaternion_multiply,
)
from mani_skill.utils.registration import *
from mani_skill.utils.structs.pose import Pose


@dataclass
class Args:
    env_id: str = "PickCube-v1"
    """the environment id"""
    n_points: int = 200
    """seed of the experiment"""
    scale_x: float = 0.1
    """total length of the prism"""
    scale_y: float = 0.1
    """total width of the prism"""
    scale_z: float = 0.1
    """total height of the prism"""
    theta: float = 0.0
    """rotation of the prism about the z axis"""
    x: float = 0.1
    """x offset from robot base"""
    y: float = 0.1
    """y offset from robot base"""
    z: float = 0.1
    """z offset from robot base"""

    # additional camera randomizations
    look_at_noise: float = 0.0
    """magnitude of uniform noise for the target in lookat transform (meters)"""
    view_axis_rot_noise: float = 0.0
    """magnitude of uniform noise rotation about the viewing axis (radians)"""

    # sample camera parameters for sapien camera sensor viewing
    n_cameras: int = 10
    """number of camera views to sample among the viewpoints"""
    target_x: float = 0.0
    """x offset from robot base for target of the lookat transformation"""
    target_y: float = 0.0
    """y offset from robot base for target of the lookat transformation"""
    target_z: float = 0.0
    """z offset from robot base for target of the lookat transformation"""
    resolution_width: int = 128
    """camera resolution width"""
    resolution_height: int = 128
    """camera resolution height"""
    fov: float = 1
    """camera fov parameter"""
    automatic_near_plane: bool = True
    """automatically calculates near plane to clip pointcloud from viewing within sensor camera viewing"""
    near: float = 0.01
    """camera near plane parameter"""
    far: float = 100
    """camera far plane parameter"""


def make_camera_rectangular_prism(
    n, scale=[0.1, 0.1, 0.1], center=[0, 0, 0], theta=0
) -> torch.Tensor:
    """
    n: number of sampled points within the geometry
    scales: [x,y,z] scale for unit cube
    center: [x,y,z] scaled unit cube coordinates
    theta: [0,2pi] rotation about the z axis
    """
    assert len(scale) == 3, len(scale)
    assert len(center) == 3, len(center)
    scale = torch.tensor(scale) if not isinstance(scale, torch.Tensor) else scale
    center = torch.tensor(center) if not isinstance(center, torch.Tensor) else center
    xyz = (torch.rand(n, 3) - 0.5) * scale
    rot_mat = euler_angles_to_matrix(
        torch.tensor([0, 0, theta], dtype=torch.float32), convention="XYZ"
    )
    return (xyz @ rot_mat.T) + center


def noised_look_at(eye, target, look_at_noise=1e-2, view_axis_rot_noise=2e-1) -> Pose:
    """
    n: number of sampled points within the geometry
    scales: [x,y,z] scale for unit cube
    center: [x,y,z] scaled unit cube coordinates
    theta: [0,2pi] rotation about the z axis
    look_at_noise: std of noise added to target in lookat transform
    view_axis_rot_noise: std of noise added to rotation about the looking direction
    """
    target = (
        torch.tensor(target).float() if not isinstance(target, torch.Tensor) else target
    )
    assert isinstance(eye, torch.Tensor)
    assert len(target) == 3
    assert target.ndim == 1
    assert isinstance(look_at_noise, float)
    assert isinstance(view_axis_rot_noise, float)

    targets = target.view(1, 3).repeat(len(eye), 1)
    noised_targets = torch.normal(mean=targets, std=look_at_noise)
    poses = sapien_utils.look_at(eye=eye, target=noised_targets)

    # axis to rotate around is the look at dirsection
    angles = torch.normal(torch.zeros(noised_targets.shape[0]), std=view_axis_rot_noise)
    axes = noised_targets - eye
    unit_axes = axes / torch.linalg.norm(axes, dim=-1).view(-1, 1)
    axis_angle = unit_axes.view(-1, 3) * angles.view(-1, 1)
    assert axis_angle.shape[0] == angles.shape[0], (axis_angle.shape, angles.shape)

    # apply the rotation after the look_at rotation, then apply lookat translation
    # look_at rotation is camera to world, it is looking down what is in axes variable
    transforms = axis_angle_to_quaternion(axis_angle)
    return Pose.create_from_pq(poses.p, q=quaternion_multiply(transforms, poses.q))


if __name__ == "__main__":
    args = tyro.cli(Args)
    assert (
        args.scale_x > 0 and args.scale_y > 0 and args.scale_z > 0
    ), "scales must be > 0"

    # points are offset by robot near eof
    scale = [args.scale_x, args.scale_y, args.scale_z]
    center = [args.x, args.y, args.z]
    target = torch.tensor([args.target_x, args.target_y, args.target_z]).float()
    points = make_camera_rectangular_prism(args.n_points, scale, center, args.theta)

    # make clipping plane further than the max distance of a ray within the volume of the cube
    if args.automatic_near_plane:
        args.near = np.linalg.norm(np.array(scale), axis=0) + 1e-6

    env_class = REGISTERED_ENVS[args.env_id].cls

    @register_env("CopyEnv-v1", max_episode_steps=1e6)
    class CopyEnv(env_class):
        def _load_scene(self, options: dict):
            super()._load_scene(options)
            self.camera_positions = []
            for i in range(len(points)):
                sphere = build_sphere(
                    self.scene,
                    radius=5e-3,
                    color=[1, 0, 0, 1],
                    name=f"c{i}_pos",
                    body_type="static",
                    add_collision=False,
                    initial_pose=(sapien.Pose(p=points[i].numpy())),
                )
                self.camera_positions.append(sphere)
            self.target_spere = build_sphere(
                self.scene,
                radius=2e-2,
                color=[0, 1, 0, 1],
                name=f"look_at_target",
                body_type="static",
                add_collision=False,
                initial_pose=(sapien.Pose(p=target)),
            )

        @property
        def _default_sensor_configs(self):
            cameras = []
            for i in range(args.n_cameras):
                pose = sapien.Pose(p=noised_poses.p[i], q=noised_poses.q[i])
                camera = CameraConfig(
                    f"camera{i}",
                    pose=pose,
                    width=args.resolution_width,
                    height=args.resolution_height,
                    fov=args.fov,
                    near=args.near,
                    far=args.far,
                )
                cameras.append(camera)
            cameras.append(
                CameraConfig(
                    f"base_camera",
                    pose=Pose.create_from_pq(p=torch.zeros(3)),
                    width=10,
                    height=10,
                    fov=1,
                )
            )
            return cameras

    # get the robot position
    original_env = gym.make(args.env_id, num_envs=1)
    original_env.reset()
    robot_pos = original_env.agent.robot.pose.p.view(3)

    # offset the points by the robot position
    points += robot_pos

    # offset the target by the robot position
    target += robot_pos

    noised_poses = noised_look_at(
        eye=points,
        target=target,
        look_at_noise=args.look_at_noise,
        view_axis_rot_noise=args.view_axis_rot_noise,
    )

    print(
        "cameras center position in env coordinates",
        np.array(center) + robot_pos.numpy(),
    )
    print("empirical", points.view(-1, 3).mean(0))
    print("cameras target position in env coordinates", np.array(target))

    env = gym.make("CopyEnv-v1", num_envs=1, render_mode="human")
    env.reset()
    while True:
        env.render()
