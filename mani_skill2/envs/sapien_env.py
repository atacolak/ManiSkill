import copy
import gc
import os
from collections import OrderedDict
from functools import cached_property
from typing import Any, Dict, List, Sequence, Tuple, Union

import gymnasium as gym
import numpy as np
import sapien
import sapien.physx
import sapien.physx as physx
import sapien.render
import sapien.utils.viewer.control_window
import torch
from gymnasium.vector.utils import batch_space
from sapien.utils import Viewer

from mani_skill2 import logger
from mani_skill2.agents.base_agent import BaseAgent
from mani_skill2.agents.multi_agent import MultiAgent
from mani_skill2.agents.robots import ROBOTS
from mani_skill2.envs.scene import ManiSkillScene
from mani_skill2.envs.utils.observations.observations import (
    sensor_data_to_pointcloud,
    sensor_data_to_rgbd,
)
from mani_skill2.sensors.base_sensor import BaseSensor, BaseSensorConfig
from mani_skill2.sensors.camera import (
    Camera,
    CameraConfig,
    parse_camera_cfgs,
    update_camera_cfgs_from_dict,
)
from mani_skill2.sensors.depth_camera import StereoDepthCamera, StereoDepthCameraConfig
from mani_skill2.utils.common import (
    convert_observation_to_space,
    dict_merge,
    flatten_state_dict,
)
from mani_skill2.utils.sapien_utils import (
    batch,
    get_obj_by_type,
    to_numpy,
    to_tensor,
    unbatch,
)
from mani_skill2.utils.structs.actor import Actor
from mani_skill2.utils.structs.articulation import Articulation
from mani_skill2.utils.structs.types import SimConfig
from mani_skill2.utils.visualization.misc import observations_to_images, tile_images


class BaseEnv(gym.Env):
    """Superclass for ManiSkill environments.

    Args:
        num_envs: number of parallel environments to run. By default this is 1, which means a CPU simulation is used. If greater than 1,
            then we initialize the GPU simulation setup. Note that not all environments are faster when simulated on the GPU due to limitations of
            GPU simulations. For example, environments with many moving objects are better simulated by parallelizing across CPUs.

        gpu_sim_backend: The GPU simulation backend to use (only used if the given num_envs argument is > 1). This affects the type of tensor
            returned by the environment for e.g. observations and rewards. Can be "torch" or "jax". Default is "torch"

        obs_mode: observation mode registered in @SUPPORTED_OBS_MODES. See TODO (stao): add doc link here about how they work.

        reward_mode: reward mode registered in @SUPPORTED_REWARD_MODES. See TODO (stao): add doc link here about how they work.

        control_mode: control mode of the agent.
            "*" represents all registered controllers, and the action space will be a dict.

        render_mode: render mode registered in @SUPPORTED_RENDER_MODES.

        shader_dir (str): shader directory. Defaults to "default".
            "default" and "rt" are built-in options with SAPIEN. Other options are user-defined.

        enable_shadow (bool): whether to enable shadow for lights. Defaults to False.

        sensor_cfgs (dict): configurations of sensors. See notes for more details.

        human_render_camera_cfgs (dict): configurations of human rendering cameras. Similar usage as @sensor_cfgs.

        robot_uids (Union[str, BaseAgent, List[Union[str, BaseAgent]]]): List of robots to instantiate and control in the environment.

        sim_cfg (dict): Configurations for simulation if used that override the environment defaults. # TODO (stao): flesh this explanation out

        reconfiguration_freq (int): How frequently to call reconfigure when environment is reset via `self.reset(...)`
            Generally for most users who are not building tasks this does not need to be changed. The default is 0, which means
            the environment reconfigures upon creation, and never again.

        force_use_gpu_sim (bool): By default this is False. If the num_envs == 1, we use GPU sim if force_use_gpu_sim is True, otherwise we use CPU sim.

    Note:
        `sensor_cfgs` is used to update environement-specific sensor configurations.
        If the key is one of sensor names (e.g. a camera), the value will be applied to the corresponding sensor.
        Otherwise, the value will be applied to all sensors (but overridden by sensor-specific values).
        # TODO (stao): add docs about sensor_cfgs, they are not as simply as dict overriding
    """

    # fmt: off
    SUPPORTED_ROBOTS: List[Union[str, Tuple[str]]] = None
    """Override this to enforce which robots or tuples of robots together are supported in the task. During env creation,
    setting robot_uids auto loads all desired robots into the scene, but not all tasks are designed to support some robot setups"""
    SUPPORTED_OBS_MODES = ("state", "state_dict", "none", "sensor_data", "rgbd", "pointcloud")
    SUPPORTED_REWARD_MODES = ("normalized_dense", "dense", "sparse")
    SUPPORTED_RENDER_MODES = ("human", "rgb_array", "sensors")
    """The supported render modes. Human opens up a GUI viewer. rgb_array returns an rgb array showing the current environment state.
    sensors returns an rgb array but only showing all data collected by sensors as images put together"""
    sim_cfg: SimConfig = SimConfig()
    # fmt: on

    metadata = {"render_modes": SUPPORTED_RENDER_MODES}

    physx_system: Union[sapien.physx.PhysxCpuSystem, sapien.physx.PhysxGpuSystem] = None

    _scene: ManiSkillScene = None
    """the main scene, which manages all sub scenes. In CPU simulation there is only one sub-scene"""

    # _agent_cls: Type[BaseAgent]
    agent: BaseAgent

    _sensors: Dict[str, BaseSensor]
    """all sensors configured in this environment"""
    _sensor_cfgs: Dict[str, BaseSensorConfig]
    """all sensor configurations"""
    _agent_camera_cfgs: Dict[str, CameraConfig]

    _human_render_cameras: Dict[str, Camera]
    """cameras used for rendering the current environment retrievable via `env.render_rgb_array()`. These are not used to generate observations"""
    _human_render_camera_cfgs: Dict[str, CameraConfig]
    """all camera configurations for cameras used for human render"""

    _hidden_objects: List[Union[Actor, Articulation]] = []
    """list of objects that are hidden during rendering when generating visual observations / running render_cameras()"""

    def __init__(
        self,
        num_envs: int = 1,
        obs_mode: str = None,
        reward_mode: str = None,
        control_mode: str = None,
        render_mode: str = None,
        shader_dir: str = "default",
        enable_shadow: bool = False,
        sensor_cfgs: dict = None,
        human_render_camera_cfgs: dict = None,
        robot_uids: Union[str, BaseAgent, List[Union[str, BaseAgent]]] = None,
        sim_cfg: SimConfig = dict(),
        reconfiguration_freq: int = 0,
        force_use_gpu_sim: bool = False,
    ):
        self.num_envs = num_envs
        self.reconfiguration_freq = reconfiguration_freq
        self._reconfig_counter = 0
        self._custom_sensor_cfgs = sensor_cfgs
        self._custom_human_render_camera_cfgs = human_render_camera_cfgs
        self.robot_uids = robot_uids
        if self.SUPPORTED_ROBOTS is not None:
            assert robot_uids in self.SUPPORTED_ROBOTS
        if num_envs > 1 or force_use_gpu_sim:
            if not sapien.physx.is_gpu_enabled():
                sapien.physx.enable_gpu()
            self.device = torch.device(
                "cuda"
            )  # TODO (stao): fix this for multi gpu support?
        else:
            self.device = torch.device("cpu")

        merged_gpu_sim_cfg = self.sim_cfg.dict()
        dict_merge(merged_gpu_sim_cfg, sim_cfg)
        self.sim_cfg = SimConfig(**merged_gpu_sim_cfg)
        # TODO (stao): there may be a memory leak or some issue with memory not being released when repeatedly creating and closing environments with high memory requirements
        # test withg pytest tests/ -m "not slow and gpu_sim" --pdb
        sapien.physx.set_gpu_memory_config(**self.sim_cfg.gpu_memory_cfg)

        self.shader_dir = shader_dir
        if self.shader_dir == "default":
            sapien.render.set_camera_shader_dir("minimal")
            sapien.render.set_picture_format("Color", "r8g8b8a8unorm")
            sapien.render.set_picture_format("ColorRaw", "r8g8b8a8unorm")
            sapien.render.set_picture_format("PositionSegmentation", "r16g16b16a16sint")
        elif self.shader_dir == "rt":
            sapien.render.set_camera_shader_dir("rt")
            sapien.render.set_viewer_shader_dir("rt")
            sapien.render.set_ray_tracing_samples_per_pixel(32)
            sapien.render.set_ray_tracing_path_depth(16)
            sapien.render.set_ray_tracing_denoiser(
                "optix"
            )  # TODO "optix or oidn?" previous value was just True
        elif self.shader_dir == "rt-fast":
            sapien.render.set_camera_shader_dir("rt")
            sapien.render.set_viewer_shader_dir("rt")
            sapien.render.set_ray_tracing_samples_per_pixel(2)
            sapien.render.set_ray_tracing_path_depth(1)
            sapien.render.set_ray_tracing_denoiser("optix")
        sapien.render.set_log_level(os.getenv("MS2_RENDERER_LOG_LEVEL", "warn"))

        # Set simulation and control frequency
        self._sim_freq = self.sim_cfg.sim_freq
        self._control_freq = self.sim_cfg.control_freq
        if self._sim_freq % self._control_freq != 0:
            logger.warning(
                f"sim_freq({self._sim_freq}) is not divisible by control_freq({self._control_freq}).",
            )
        self._sim_steps_per_control = self._sim_freq // self._control_freq

        # Observation mode
        if obs_mode is None:
            obs_mode = self.SUPPORTED_OBS_MODES[0]
        if obs_mode not in self.SUPPORTED_OBS_MODES:
            raise NotImplementedError("Unsupported obs mode: {}".format(obs_mode))
        self._obs_mode = obs_mode

        # Reward mode
        if reward_mode is None:
            reward_mode = self.SUPPORTED_REWARD_MODES[0]
        if reward_mode not in self.SUPPORTED_REWARD_MODES:
            raise NotImplementedError("Unsupported reward mode: {}".format(reward_mode))
        self._reward_mode = reward_mode

        # Control mode
        self._control_mode = control_mode
        # TODO(jigu): Support dict action space
        if control_mode == "*":
            raise NotImplementedError("Multiple controllers are not supported yet.")

        # Render mode
        self.render_mode = render_mode
        self._viewer = None

        # Lighting
        self.enable_shadow = enable_shadow

        # Use a fixed (main) seed to enhance determinism
        self._main_seed = None
        self._set_main_rng(2022)
        self._elapsed_steps = (
            torch.zeros(self.num_envs, device=self.device, dtype=torch.int32)
            if physx.is_gpu_enabled()
            else 0
        )
        obs, _ = self.reset(seed=2022, options=dict(reconfigure=True))
        if physx.is_gpu_enabled():
            obs = to_numpy(obs)
        self._init_raw_obs = obs.copy()
        """the raw observation returned by the env.reset. Useful for future observation wrappers to use to auto generate observation spaces"""
        # TODO handle constructing single obs space from a batched result.

        self.action_space = self.agent.action_space
        self.single_action_space = self.agent.single_action_space
        self._orig_single_action_space = copy.deepcopy(self.single_action_space)
        # initialize the cached properties
        self.single_observation_space
        self.observation_space

    def _update_obs_space(self, obs: Any):
        """call this function if you modify the observations returned by env.step and env.reset via an observation wrapper. The given observation must be a numpy array"""
        self._init_raw_obs = obs
        del self.single_observation_space
        del self.observation_space
        self.single_observation_space
        self.observation_space

    @cached_property
    def single_observation_space(self):
        if self.num_envs > 1:
            return convert_observation_to_space(self._init_raw_obs, unbatched=True)
        else:
            return convert_observation_to_space(self._init_raw_obs)

    @cached_property
    def observation_space(self):
        if self.num_envs > 1:
            return batch_space(self.single_observation_space, n=self.num_envs)
        else:
            return self.single_observation_space

    def _load_agent(self):
        # agent_cls: Type[BaseAgent] = self._agent_cls
        agents = []
        robot_uids = self.robot_uids
        if robot_uids is not None:
            if not isinstance(robot_uids, tuple):
                robot_uids = [robot_uids]
            for i, robot_uids in enumerate(robot_uids):
                if isinstance(robot_uids, type(BaseAgent)):
                    agent_cls = robot_uids
                    # robot_uids = self._agent_cls.uid
                else:
                    agent_cls = ROBOTS[robot_uids]
                agent: BaseAgent = agent_cls(
                    self._scene,
                    self._control_freq,
                    self._control_mode,
                    agent_idx=i if len(robot_uids) > 0 else None,
                )
                agent.set_control_mode()
                agents.append(agent)
        if len(agents) == 1:
            self.agent = agents[0]
        else:
            self.agent = MultiAgent(agents)
        # set_articulation_render_material(self.agent.robot, specular=0.9, roughness=0.3)

    def _configure_sensors(self):
        self._sensor_cfgs = OrderedDict()

        # Add task/external sensors
        self._sensor_cfgs.update(parse_camera_cfgs(self._register_sensors()))

        # Add agent sensors
        self._agent_camera_cfgs = OrderedDict()
        self._agent_camera_cfgs = parse_camera_cfgs(self.agent.sensor_configs)
        self._sensor_cfgs.update(self._agent_camera_cfgs)

    def _register_sensors(
        self,
    ) -> Union[
        BaseSensorConfig, Sequence[BaseSensorConfig], Dict[str, BaseSensorConfig]
    ]:
        """Register (non-agent) sensors for the environment."""
        return []

    def _configure_human_render_cameras(self):
        self._human_render_camera_cfgs = parse_camera_cfgs(
            self._register_human_render_cameras()
        )

    def _register_human_render_cameras(
        self,
    ) -> Union[
        BaseSensorConfig, Sequence[BaseSensorConfig], Dict[str, BaseSensorConfig]
    ]:
        """Register cameras for rendering."""
        return []

    @property
    def sim_freq(self):
        return self._sim_freq

    @property
    def control_freq(self):
        return self._control_freq

    @property
    def sim_timestep(self):
        return 1.0 / self._sim_freq

    @property
    def control_timestep(self):
        return 1.0 / self._control_freq

    @property
    def control_mode(self):
        return self.agent.control_mode

    @property
    def elapsed_steps(self):
        return self._elapsed_steps

    # ---------------------------------------------------------------------------- #
    # Observation
    # ---------------------------------------------------------------------------- #
    @property
    def obs_mode(self):
        return self._obs_mode

    def get_obs(self, info: Dict = None):
        """
        Return the current observation of the environment. User may call this directly to get the current observation
        as opposed to taking a step with actions in the environment.

        Note that some tasks use info of the current environment state to populate the observations to avoid having to
        compute slow operations twice. For example a state based observation may wish to include a boolean indicating
        if a robot is grasping an object. Computing this boolean correctly is slow, so it is preferable to generate that
        data in the info object by overriding the `self.evaluate` function.

        Args:
            info (Dict): The info object of the environment. Generally should always be the result of `self.get_info()`.
                If this is None (the default), this function will call `self.get_info()` itself
        """
        squeeze_dims = self.num_envs == 1
        if info is None:
            info = self.get_info()
        if self._obs_mode == "none":
            # Some cases do not need observations, e.g., MPC
            return OrderedDict()
        elif self._obs_mode == "state":
            state_dict = self._get_obs_state_dict(info)
            obs = flatten_state_dict(state_dict, use_torch=True, device=self.device)
        elif self._obs_mode == "state_dict":
            obs = self._get_obs_state_dict(info)
        elif self._obs_mode in ["sensor_data", "rgbd", "pointcloud"]:
            obs = self._get_obs_with_sensor_data(info)
            if self._obs_mode == "rgbd":
                obs = sensor_data_to_rgbd(obs, self._sensors)
            elif self.obs_mode == "pointcloud":
                obs = sensor_data_to_pointcloud(obs, self._sensors)
        else:
            raise NotImplementedError(self._obs_mode)
        return obs

    def _get_obs_state_dict(self, info: Dict):
        """Get (ground-truth) state-based observations."""
        return OrderedDict(
            agent=self._get_obs_agent(),
            extra=self._get_obs_extra(info),
        )

    def _get_obs_agent(self):
        """Get observations from the agent's sensors, e.g., proprioceptive sensors."""
        return self.agent.get_proprioception()

    def _get_obs_extra(self, info: Dict):
        """Get task-relevant extra observations."""
        return OrderedDict()

    def update_render(self):
        """Update renderer(s). This function should be called before any rendering,
        to sync simulator and renderer."""
        # TODO (stao): note that update_render has some overhead. Currently when using image observation mode + using render() for recording videos
        # this is called twice

        # TODO (stao): We might want to factor out some of this code below
        self._scene.update_render()

    def capture_sensor_data(self):
        """Capture data from all sensors (non-blocking)"""
        for sensor in self._sensors.values():
            sensor.capture()

    def get_sensor_obs(self) -> Dict[str, Dict[str, torch.Tensor]]:
        """Get raw sensor data for use as observations."""
        sensor_data = OrderedDict()
        for name, sensor in self._sensors.items():
            sensor_data[name] = sensor.get_obs()
        return sensor_data

    def get_sensor_images(self) -> Dict[str, Dict[str, torch.Tensor]]:
        """Get raw sensor data as images for visualization purposes."""
        sensor_data = OrderedDict()
        for name, sensor in self._sensors.items():
            sensor_data[name] = sensor.get_images()
        return sensor_data

    def get_sensor_params(self) -> Dict[str, Dict[str, torch.Tensor]]:
        """Get all sensor parameters."""
        params = OrderedDict()
        for name, sensor in self._sensors.items():
            params[name] = sensor.get_params()
        return params

    def _get_obs_with_sensor_data(self, info: Dict) -> OrderedDict:
        for obj in self._hidden_objects:
            obj.hide_visual()
        self.update_render()
        self.capture_sensor_data()
        return OrderedDict(
            agent=self._get_obs_agent(),
            extra=self._get_obs_extra(info),
            sensor_param=self.get_sensor_params(),
            sensor_data=self.get_sensor_obs(),
        )

    @property
    def robot_link_ids(self):
        """Get link ids for the robot. This is used for segmentation observations."""
        return self.agent.robot_link_ids

    # -------------------------------------------------------------------------- #
    # Reward mode
    # -------------------------------------------------------------------------- #
    @property
    def reward_mode(self):
        return self._reward_mode

    def get_reward(self, obs: Any, action: torch.Tensor, info: Dict):
        if self._reward_mode == "sparse":
            reward = info["success"]
        elif self._reward_mode == "dense":
            reward = self.compute_dense_reward(obs=obs, action=action, info=info)
        elif self._reward_mode == "normalized_dense":
            reward = self.compute_normalized_dense_reward(
                obs=obs, action=action, info=info
            )
        else:
            raise NotImplementedError(self._reward_mode)
        return reward

    def compute_dense_reward(self, obs: Any, action: torch.Tensor, info: Dict):
        raise NotImplementedError

    def compute_normalized_dense_reward(
        self, obs: Any, action: torch.Tensor, info: Dict
    ):
        raise NotImplementedError

    # -------------------------------------------------------------------------- #
    # Reconfigure
    # -------------------------------------------------------------------------- #
    def reconfigure(self):
        """Reconfigure the simulation scene instance.
        This function clears the previous scene and creates a new one.

        Note this function is not always called when an environment is reset, and
        should only be used if any agents, assets, sensors, lighting need to change
        to save compute time.

        Tasks like PegInsertionSide and TurnFaucet will call this each time as the peg
        shape changes each time and the faucet model changes each time respectively.
        """

        with torch.random.fork_rng():
            torch.manual_seed(seed=self._episode_seed)
            self._clear()
            # load everything into the scene first before initializing anything
            self._setup_scene()
            self._load_agent()
            self._load_actors()
            self._load_articulations()

            self._setup_lighting()

            # NOTE(jigu): Agent and camera configurations should not change after initialization.
            self._configure_sensors()
            self._configure_human_render_cameras()

            # TODO (stao): permit camera changes on env creation here
            # # Override camera configurations
            if self._custom_sensor_cfgs is not None:
                update_camera_cfgs_from_dict(
                    self._sensor_cfgs, self._custom_sensor_cfgs
                )
            if self._custom_human_render_camera_cfgs is not None:
                update_camera_cfgs_from_dict(
                    self._human_render_camera_cfgs,
                    self._custom_human_render_camera_cfgs,
                )

            # Cache entites and articulations
            if sapien.physx.is_gpu_enabled():
                self._scene._setup_gpu()
                self._scene._gpu_fetch_all()
            self._setup_sensors()  # for GPU sim, we have to setup sensors after we call setup gpu in order to enable loading mounted sensors
            if self._viewer is not None:
                self._setup_viewer()
        self._reconfig_counter = self.reconfiguration_freq

    def _load_actors(self):
        """Loads all actors into the scene. Called by `self.reconfigure`"""

    def _load_articulations(self):
        """Loads all articulations into the scene. Called by `self.reconfigure`"""

    # TODO (stao): refactor this into sensor API
    def _setup_sensors(self):
        """Setup sensors in the scene. Called by `self.reconfigure`"""
        self._sensors = OrderedDict()

        for uid, sensor_cfg in self._sensor_cfgs.items():
            if uid in self._agent_camera_cfgs:
                articulation = self.agent.robot
            else:
                articulation = None
            if isinstance(sensor_cfg, StereoDepthCameraConfig):
                sensor_cls = StereoDepthCamera
            else:
                sensor_cls = Camera
            self._sensors[uid] = sensor_cls(
                sensor_cfg,
                self._scene,
                articulation=articulation,
            )

        # Cameras for rendering only
        self._human_render_cameras = OrderedDict()
        for uid, camera_cfg in self._human_render_camera_cfgs.items():
            self._human_render_cameras[uid] = Camera(
                camera_cfg,
                self._scene,
            )

        self._scene.sensors = self._sensors
        self._scene.human_render_cameras = self._human_render_cameras

    def _setup_lighting(self):
        # TODO (stao): remove this code out. refactor it to be inside scene builders
        """Setup lighting in the scene. Called by `self.reconfigure`"""

        shadow = self.enable_shadow
        self._scene.set_ambient_light([0.3, 0.3, 0.3])
        # Only the first of directional lights can have shadow
        self._scene.add_directional_light(
            [1, 1, -1], [1, 1, 1], shadow=shadow, shadow_scale=5, shadow_map_size=2048
        )
        self._scene.add_directional_light([0, 0, -1], [1, 1, 1])

    # -------------------------------------------------------------------------- #
    # Reset
    # -------------------------------------------------------------------------- #
    def reset(self, seed=None, options=None):
        """
        Reset the ManiSkill environment

        Note that ManiSkill always holds two RNG states, a main RNG, and an episode RNG. The main RNG is used purely to sample an episode seed which
        helps with reproducibility of episodes. The episode RNG is used by the environment/task itself to e.g. randomize object positions, randomize assets etc.

        Upon environment creation via gym.make, the main RNG is set with a fixed seed of 2022.
        During each reset call, if seed is None, main RNG is unchanged and an episode seed is sampled from the main RNG to create the episode RNG.
        If seed is not None, main RNG is set to that seed and the episode seed is also set to that seed.


        Note that when giving a specific seed via `reset(seed=...)`, we always set the main RNG based on that seed. This then deterministically changes the **sequence** of RNG
        used for each episode after each call to reset with `seed=None`. By default this sequence of rng starts with the default main seed used which is 2022,
        which means that when creating an environment and resetting without a seed, it will always have the same sequence of RNG for each episode.

        """
        if options is None:
            options = dict()

        self._set_main_rng(seed)
        # we first set the first episode seed to allow environments to use it to reconfigure the environment with a seed
        self._set_episode_rng(seed)

        reconfigure = options.get("reconfigure", False)
        reconfigure = reconfigure or (
            self._reconfig_counter == 0 and self.reconfiguration_freq != 0
        )
        if reconfigure:
            self.reconfigure()

        if "env_idx" in options:
            env_idx = options["env_idx"]
            self._scene._reset_mask = torch.zeros(
                self.num_envs, dtype=bool, device=self.device
            )
            self._scene._reset_mask[env_idx] = True
        else:
            env_idx = torch.arange(0, self.num_envs, device=self.device)
            self._scene._reset_mask = torch.ones(
                self.num_envs, dtype=bool, device=self.device
            )
        if physx.is_gpu_enabled():
            self._elapsed_steps[env_idx] = 0
        else:
            self._elapsed_steps = 0

        if not reconfigure:
            self._clear_sim_state()
        if self.reconfiguration_freq != 0:
            self._reconfig_counter -= 1
        # Set the episode rng again after reconfiguration to guarantee seed reproducibility
        self._set_episode_rng(self._episode_seed)
        self.agent.reset()
        self.initialize_episode(env_idx)
        obs = self.get_obs()
        if physx.is_gpu_enabled():
            # ensure all updates to object poses and configurations are applied on GPU after task initialization
            self._scene._gpu_apply_all()
            self._scene.px.gpu_update_articulation_kinematics()
            self._scene._gpu_fetch_all()
        else:
            obs = to_numpy(unbatch(obs))
            self._elapsed_steps = 0
        return obs, {}

    def _set_main_rng(self, seed):
        """Set the main random generator which is only used to set the seed of the episode RNG to improve reproducibility.

        Note that while _set_main_rng and _set_episode_rng are setting a seed and numpy random state, when using GPU sim
        parallelization it is highly recommended to use torch random functions as they will make things run faster. The use
        of torch random functions when building tasks in ManiSkill are automatically seeded via `torch.random.fork`
        """
        if seed is None:
            if self._main_seed is not None:
                return
            seed = np.random.RandomState().randint(2**32)
        self._main_seed = seed
        self._main_rng = np.random.RandomState(self._main_seed)

    def _set_episode_rng(self, seed):
        """Set the random generator for current episode."""
        if seed is None:
            self._episode_seed = self._main_rng.randint(2**32)
        else:
            self._episode_seed = seed
        self._episode_rng = np.random.RandomState(self._episode_seed)

    def initialize_episode(self, env_idx: torch.Tensor):
        # TODO (stao): should we even split these into 4 separate functions?
        """Initialize the episode, e.g., poses of entities and articulations, and robot configuration.
        No new assets are created. Task-relevant information can be initialized here, like goals.
        """
        with torch.random.fork_rng():
            torch.manual_seed(self._episode_seed)
            self._initialize_actors(env_idx)
            self._initialize_articulations(env_idx)
            self._initialize_agent(env_idx)
            self._initialize_task(env_idx)

    def _initialize_actors(self, env_idx: torch.Tensor):
        """Initialize the poses of actors. Called by `self.initialize_episode`"""

    def _initialize_articulations(self, env_idx: torch.Tensor):
        """Initialize the (joint) poses of articulations. Called by `self.initialize_episode`"""

    def _initialize_agent(self, env_idx: torch.Tensor):
        """Initialize the (joint) poses of agent(robot). Called by `self.initialize_episode`"""

    def _initialize_task(self, env_idx: torch.Tensor):
        """Initialize task-relevant information, like goals. Called by `self.initialize_episode`"""

    def _clear_sim_state(self):
        # TODO (stao): we should rename this. This could mean setting pose to 0 as if we just reconfigured everything...
        """Clear simulation state (velocities)"""
        for actor in self._scene.actors.values():
            if actor.px_body_type == "static":
                continue
            actor.set_linear_velocity([0, 0, 0])
            actor.set_angular_velocity([0, 0, 0])
        for articulation in self._scene.articulations.values():
            articulation.set_qvel(np.zeros(articulation.max_dof))
            articulation.set_root_linear_velocity([0, 0, 0])
            articulation.set_root_angular_velocity([0, 0, 0])
        if physx.is_gpu_enabled():
            self._scene._gpu_apply_all()
            self._scene._gpu_fetch_all()
            # TODO (stao): This may be an unnecessary fetch and apply. ALSO do not fetch right after apply, no guarantee the data is updated correctly

    # -------------------------------------------------------------------------- #
    # Step
    # -------------------------------------------------------------------------- #

    def step(self, action: Union[None, np.ndarray, torch.Tensor, Dict]):
        action = self.step_action(action)
        self._elapsed_steps += 1
        info = self.get_info()
        obs = self.get_obs(info)
        reward = self.get_reward(obs=obs, action=action, info=info)
        if "success" in info:
            if "fail" in info:
                terminated = torch.logical_or(info["success"], info["fail"])
            else:
                terminated = info["success"]
        else:
            if "fail" in info:
                terminated = info["success"]
            else:
                terminated = torch.zeros(self.num_envs, dtype=bool, device=self.device)

        if physx.is_gpu_enabled():
            return (
                obs,
                reward,
                terminated,
                torch.zeros(self.num_envs, device=self.device),
                info,
            )
        else:
            # On CPU sim mode, we always return numpy / python primitives without any batching.
            return unbatch(
                to_numpy(obs),
                to_numpy(reward),
                to_numpy(terminated),
                False,
                to_numpy(info),
            )

    def step_action(
        self, action: Union[None, np.ndarray, torch.Tensor, Dict]
    ) -> Union[None, torch.Tensor]:
        set_action = False
        action_is_unbatched = False
        if action is None:  # simulation without action
            pass
        elif isinstance(action, np.ndarray) or isinstance(action, torch.Tensor):
            action = to_tensor(action)
            if action.shape == self._orig_single_action_space.shape:
                action_is_unbatched = True
            set_action = True
        elif isinstance(action, dict):
            if "control_mode" in action:
                if action["control_mode"] != self.agent.control_mode:
                    self.agent.set_control_mode(action["control_mode"])
                    self.agent.controller.reset()
                action = to_tensor(action["action"])
            else:
                assert isinstance(
                    self.agent, MultiAgent
                ), "Received a dictionary for an action but there are not multiple robots in the environment"
                # assume this is a multi-agent action
                action = to_tensor(action)
                for k, a in action.items():
                    if a.shape == self._orig_single_action_space[k].shape:
                        action_is_unbatched = True
                        break
            set_action = True
        else:
            raise TypeError(type(action))

        if set_action:
            if self.num_envs == 1 and action_is_unbatched:
                action = batch(action)
            self.agent.set_action(action)
            if physx.is_gpu_enabled():
                self._scene.px.gpu_apply_articulation_target_position()
                self._scene.px.gpu_apply_articulation_target_velocity()
        self._before_control_step()
        for _ in range(self._sim_steps_per_control):
            self.agent.before_simulation_step()
            with sapien.profile("step_i"):
                self._scene.step()
            self._after_simulation_step()
        if physx.is_gpu_enabled():
            self._scene._gpu_fetch_all()
        return action

    def evaluate(self) -> dict:
        """
        Evaluate whether the environment is currently in a success state by returning a dictionary with a "success" key or
        a failure state via a "fail" key

        This function may also return additional data that has been computed (e.g. is the robot grasping some object) that may be
        reused when generating observations and rewards.
        """
        raise NotImplementedError

    def get_info(self):
        """
        Get info about the current environment state, include elapsed steps and evaluation information
        """
        info = dict(elapsed_steps=self._elapsed_steps.clone())
        info.update(self.evaluate())
        return info

    def _before_control_step(self):
        pass

    def _after_simulation_step(self):
        pass

    # -------------------------------------------------------------------------- #
    # Simulation and other gym interfaces
    # -------------------------------------------------------------------------- #
    def _set_scene_config(self):
        # TODO (stao): Do these have any effect after calling gpu_init?
        physx.set_scene_config(**self.sim_cfg.scene_cfg)
        physx.set_default_material(**self.sim_cfg.default_materials_cfg)

    def _setup_scene(self):
        """Setup the simulation scene instance.
        The function should be called in reset(). Called by `self.reconfigure`"""
        self._set_scene_config()
        if sapien.physx.is_gpu_enabled():
            self.physx_system = sapien.physx.PhysxGpuSystem()
            # Create the scenes in a square grid
            sub_scenes = []
            scene_grid_length = int(np.ceil(np.sqrt(self.num_envs)))
            for scene_idx in range(self.num_envs):
                scene_x, scene_y = (
                    scene_idx % scene_grid_length,
                    scene_idx // scene_grid_length,
                )
                scene = sapien.Scene(
                    systems=[self.physx_system, sapien.render.RenderSystem()]
                )
                scene.physx_system.set_scene_offset(
                    scene,
                    [
                        scene_x * self.sim_cfg.spacing,
                        scene_y * self.sim_cfg.spacing,
                        0,
                    ],
                )
                sub_scenes.append(scene)
        else:
            self.physx_system = sapien.physx.PhysxCpuSystem()
            sub_scenes = [
                sapien.Scene([self.physx_system, sapien.render.RenderSystem()])
            ]
        # create a "global" scene object that users can work with that is linked with all other scenes created
        self._scene = ManiSkillScene(sub_scenes, device=self.device)
        self.physx_system.timestep = 1.0 / self._sim_freq

    def _clear(self):
        """Clear the simulation scene instance and other buffers.
        The function can be called in reset() before a new scene is created.
        Called by `self.reconfigure` and when the environment is closed/deleted
        """
        self._close_viewer()
        self.agent = None
        self._sensors = OrderedDict()
        self._human_render_cameras = OrderedDict()
        self._scene = None
        self._hidden_objects = []

    def close(self):
        self._clear()
        gc.collect()  # force gc to collect which releases most GPU memory

    def _close_viewer(self):
        if self._viewer is None:
            return
        self._viewer.close()
        self._viewer = None

    # -------------------------------------------------------------------------- #
    # Simulation state (required for MPC)
    # -------------------------------------------------------------------------- #
    def get_actors(self) -> List[sapien.Entity]:
        return self._scene.get_all_actors()

    def get_articulations(self) -> List[physx.PhysxArticulation]:
        articulations = self._scene.get_all_articulations()
        # NOTE(jigu): There might be dummy articulations used by controllers.
        # TODO(jigu): Remove dummy articulations if exist.
        return articulations

    def get_state(self):
        """Get environment state. Override to include task information (e.g., goal)"""
        state = self._scene.get_sim_state()
        if physx.is_gpu_enabled():
            return state
        return state[0]

    def set_state(self, state: np.ndarray):
        """Set environment state. Override to include task information (e.g., goal)"""
        if len(state.shape) == 1:
            state = batch(state)
        self._scene.set_sim_state(state)
        if physx.is_gpu_enabled():
            self._scene._gpu_apply_all()
            self._scene.px.gpu_update_articulation_kinematics()
            self._scene._gpu_fetch_all()

    # -------------------------------------------------------------------------- #
    # Visualization
    # -------------------------------------------------------------------------- #
    @property
    def viewer(self):
        return self._viewer

    def _setup_viewer(self):
        """Setup the interactive viewer.

        The function should be called after a new scene is configured.
        In subclasses, this function can be overridden to set viewer cameras.

        Called by `self.reconfigure`
        """
        # TODO (stao): handle GPU parallel sim rendering code:
        if physx.is_gpu_enabled():
            self._viewer_scene_idx = 0
        # CAUTION: `set_scene` should be called after assets are loaded.
        self._viewer.set_scene(self._scene.sub_scenes[0])
        control_window: sapien.utils.viewer.control_window.ControlWindow = (
            get_obj_by_type(
                self._viewer.plugins, sapien.utils.viewer.control_window.ControlWindow
            )
        )
        control_window.show_joint_axes = False
        control_window.show_camera_linesets = False

    def render_human(self):
        if self._viewer is None:
            self._viewer = Viewer()
            self._setup_viewer()
            self._viewer.set_camera_pose(
                self._human_render_cameras["render_camera"].camera.global_pose
            )
        for obj in self._hidden_objects:
            obj.show_visual()
        if physx.is_gpu_enabled() and self._scene._gpu_sim_initialized:
            self.physx_system.sync_poses_gpu_to_cpu()
        self._viewer.render()
        return self._viewer

    def render_rgb_array(self, camera_name: str = None):
        """Returns an RGB array / image of size (num_envs, H, W, 3) of the current state of the environment.
        This is captured by any of the registered human render cameras. If a camera_name is given, only data from that camera is returned.
        Otherwise all camera data is captured and returned as a single batched image"""
        for obj in self._hidden_objects:
            obj.show_visual()
        self.update_render()
        images = []
        # TODO (stao): refactor this code either into ManiSkillScene class and/or merge the code, it's pretty similar?
        if physx.is_gpu_enabled():
            for name in self._scene.human_render_cameras.keys():
                camera_group = self._scene.camera_groups[name]
                if camera_name is not None and name != camera_name:
                    continue
                camera_group.take_picture()
                rgb = camera_group.get_picture_cuda("Color").torch()[..., :3].clone()
                images.append(rgb)
        else:
            for name, camera in self._scene.human_render_cameras.items():
                if camera_name is not None and name != camera_name:
                    continue
                camera.capture()
                # TODO (stao): the output of this is not the same as gpu setting, its float here
                if self.shader_dir == "default":
                    rgb = (camera.get_picture("Color")[..., :3]).to(torch.uint8)
                else:
                    rgb = (camera.get_picture("Color")[..., :3] * 255).to(torch.uint8)
                images.append(rgb)
        if len(images) == 0:
            return None
        if len(images) == 1:
            return images[0]
        return tile_images(images)

    def render_sensors(self):
        """
        Renders all sensors that the agent can use and see and displays them
        """
        images = []
        for obj in self._hidden_objects:
            obj.hide_visual()
        self.update_render()
        self.capture_sensor_data()
        sensor_images = self.get_sensor_obs()
        for sensor_images in sensor_images.values():
            images.extend(observations_to_images(sensor_images))
        return tile_images(images)

    def render(self):
        """
        Either opens a viewer if render_mode is "human", or returns an array that you can use to save videos.

        render_mode is "rgb_array", usually a higher quality image is rendered for the purpose of viewing only.

        if render_mode is "sensors", all visual observations the agent can see is provided
        """
        if self.render_mode is None:
            raise RuntimeError("render_mode is not set.")
        if self.render_mode == "human":
            return self.render_human()
        elif self.render_mode == "rgb_array":
            return self.render_rgb_array()
        elif self.render_mode == "sensors":
            return self.render_sensors()
        else:
            raise NotImplementedError(f"Unsupported render mode {self.render_mode}.")

    # TODO (stao): re implement later
    # # ---------------------------------------------------------------------------- #
    # # Advanced
    # # ---------------------------------------------------------------------------- #
    # def gen_scene_pcd(self, num_points: int = int(1e5)) -> np.ndarray:
    #     """Generate scene point cloud for motion planning, excluding the robot"""
    #     meshes = []
    #     articulations = self._scene.get_all_articulations()
    #     if self.agent is not None:
    #         articulations.pop(articulations.index(self.agent.robot))
    #     for articulation in articulations:
    #         articulation_mesh = merge_meshes(get_articulation_meshes(articulation))
    #         if articulation_mesh:
    #             meshes.append(articulation_mesh)

    #     for actor in self._scene.get_all_actors():
    #         actor_mesh = merge_meshes(get_component_meshes(actor))
    #         if actor_mesh:
    #             meshes.append(
    #                 actor_mesh.apply_transform(
    #                     actor.get_pose().to_transformation_matrix()
    #                 )
    #             )

    #     scene_mesh = merge_meshes(meshes)
    #     scene_pcd = scene_mesh.sample(num_points)
    #     return scene_pcd
