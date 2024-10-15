from typing import Dict

import numpy as np
import sapien
import torch
from transforms3d.euler import euler2quat

from mani_skill.agents.robots.panda.panda_stick import PandaStick
from mani_skill.envs.sapien_env import BaseEnv
from mani_skill.sensors.camera import CameraConfig
from mani_skill.utils import sapien_utils
from mani_skill.utils.registration import register_env
from mani_skill.utils.scene_builder.table.scene_builder import TableSceneBuilder
from mani_skill.utils.structs.actor import Actor
from mani_skill.utils.structs.pose import Pose
from mani_skill.utils.structs.types import SceneConfig, SimConfig


@register_env("CleanSpots-v1", max_episode_steps=200)
class CleanSpotsEnv(BaseEnv):
    MAX_SPOTS = 5

    DOT_THICKNESS = 0.003
    """thickness of the paint drawn on to the canvas"""
    CANVAS_THICKNESS = 0.02
    """How thick the canvas on the table is"""
    BRUSH_RADIUS = 0.01
    """The brushes radius"""
    BRUSH_COLORS = [[0.8, 0.2, 0.2, 1]]
    """The colors of the brushes. If there is more than one color, each parallel environment will have a randomly sampled color."""

    SUPPORTED_REWARD_MODES = ["none"]

    SUPPORTED_ROBOTS: ["panda_stick"]
    agent: PandaStick

    def __init__(self, *args, robot_uids="panda_stick", **kwargs):
        super().__init__(*args, robot_uids=robot_uids, **kwargs)

    @property
    def _default_sim_config(self):
        # we set contact_offset to a small value as we are not expecting to make any contacts really apart from the brush hitting the canvas too hard.
        # We set solver iterations very low as this environment is not doing a ton of manipulation (the brush is attached to the robot after all)
        return SimConfig(
            sim_freq=100,
            control_freq=20,
            scene_config=SceneConfig(
                contact_offset=0.01,
                solver_position_iterations=4,
                solver_velocity_iterations=0,
            ),
        )

    @property
    def _default_sensor_configs(self):
        pose = sapien_utils.look_at(eye=[0.3, 0, 0.8], target=[0, 0, 0.1])
        return [
            CameraConfig(
                "base_camera",
                pose=pose,
                width=320,
                height=240,
                fov=1.2,
                near=0.01,
                far=100,
            )
        ]

    @property
    def _default_human_render_camera_configs(self):
        pose = sapien_utils.look_at(eye=[0.3, 0, 0.8], target=[0, 0, 0.1])
        return CameraConfig(
            "render_camera",
            pose=pose,
            width=1280,
            height=960,
            fov=1.2,
            near=0.01,
            far=100,
        )

    def _load_scene(self, options: dict):
        self.table_scene = TableSceneBuilder(self, robot_init_qpos_noise=0)
        self.table_scene.build()
        for part in self.table_scene.table._objs:
            for triangle in (
                part.find_component_by_type(sapien.render.RenderBodyComponent)
                .render_shapes[0]
                .parts
            ):
                triangle.material.set_base_color(np.array([255, 255, 255, 255]) / 255)
                triangle.material.set_base_color_texture(None)
                triangle.material.set_normal_texture(None)
                triangle.material.set_emission_texture(None)
                triangle.material.set_transmission_texture(None)
                triangle.material.set_metallic_texture(None)
                triangle.material.set_roughness_texture(None)

        self.spots = []
        for i in range(self.MAX_SPOTS):
            builder = self.scene.create_actor_builder()
            builder.add_cylinder_visual(
                radius=self.BRUSH_RADIUS,
                half_length=self.DOT_THICKNESS / 2,
                material=sapien.render.RenderMaterial(base_color=self.BRUSH_COLORS[0]),
            )
            actor = builder.build_kinematic(name=f"spot_{i}")
            self.spots.append(actor)

    def _initialize_episode(self, env_idx: torch.Tensor, options: dict):
        # NOTE (stao): for simplicity this task cannot handle partial resets
        self.draw_step = 0
        with torch.device(self.device):
            self.table_scene.initialize(env_idx)
            for spot in self.spots:
                spot.set_pose(sapien.Pose(p=[0, 0, 0], q=euler2quat(0, np.pi / 2, 0)))

    def _after_control_step(self):
        if self.gpu_sim_enabled:
            self.scene._gpu_fetch_all()
        robot_touching_table = (
            self.agent.tcp.pose.p[:, 2]
            < self.CANVAS_THICKNESS + self.DOT_THICKNESS + 0.005
        )
        robot_brush_pos = torch.zeros((self.num_envs, 3), device=self.device)
        robot_brush_pos[:, 2] = -self.DOT_THICKNESS
        robot_brush_pos[robot_touching_table, :2] = self.agent.tcp.pose.p[
            robot_touching_table, :2
        ]
        # robot_brush_pos[robot_touching_table, 2] = (
        #     self.DOT_THICKNESS / 2 + self.CANVAS_THICKNESS
        # )
        # # move the next unused dot to the robot's brush position. All unused dots are initialized inside the table so they aren't visible
        # self.spots[self.draw_step].set_pose(
        #     Pose.create_from_pq(robot_brush_pos, euler2quat(0, np.pi / 2, 0))
        # )
        # self.draw_step += 1

        # on GPU sim we have to call _gpu_apply_all() to apply the changes we make to object poses.
        if self.gpu_sim_enabled:
            self.scene._gpu_apply_all()

    def evaluate(self):
        return {}

    def _get_obs_extra(self, info: Dict):
        return dict(
            tcp_pose=self.agent.tcp.pose.raw_pose,
        )