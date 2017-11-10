import os.path as osp
import os, datetime, sys
import gym
import fnmatch
import time
import joblib
import logging
import numpy as np
import tensorflow as tf
from baselines import logger

from baselines.common import set_global_seeds, explained_variance
from baselines.common.vec_env.subproc_vec_env import SubprocVecEnv
from baselines.common.atari_wrappers import wrap_deepmind

from baselines.a2c.utils import discount_with_dones
from baselines.a2c.utils import Scheduler, make_path, find_trainable_variables
from baselines.a2c.policies import CnnPolicy
from baselines.a2c.utils import cat_entropy, mse

class Model(object):

    def __init__(self, policy, ob_space, ac_space, nenvs, nsteps, nstack, num_procs,
            pg_coef=1.0, ent_coef=0.01, vf_coef=0.5, max_grad_norm=0.5, lr=7e-4,
            alpha=0.99, epsilon=1e-5, total_timesteps=int(80e6), lrschedule='linear', nModelsToKeep=5):
        config = tf.ConfigProto(allow_soft_placement=True,
                                intra_op_parallelism_threads=num_procs,
                                inter_op_parallelism_threads=num_procs)
        config.gpu_options.allow_growth = True
        sess = tf.Session(config=config)
        nact = ac_space.n
        nbatch = nenvs*nsteps

        A = tf.placeholder(tf.int32, [nbatch])
        ADV = tf.placeholder(tf.float32, [nbatch])
        R = tf.placeholder(tf.float32, [nbatch])
        LR = tf.placeholder(tf.float32, [])

        step_model = policy(sess, ob_space, ac_space, nenvs, 1, nstack, reuse=False)
        train_model = policy(sess, ob_space, ac_space, nenvs, nsteps, nstack, reuse=True)

        selected_idx = tf.stack([tf.range(0,tf.cast(A.get_shape()[0],dtype=tf.int32), dtype=tf.int32), A], axis=1)
        q_acted = tf.gather_nd(train_model.q, selected_idx)

        neglogpac = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi_logits, labels=A)
        pg_loss = tf.reduce_mean(ADV * neglogpac)
        vf_loss = tf.reduce_mean(mse(q_acted, R))
        entropy = tf.reduce_mean(cat_entropy(train_model.pi_logits))
        loss = pg_loss*pg_coef - entropy*ent_coef + vf_loss * vf_coef

        params = find_trainable_variables("model")
        grads = tf.gradients(loss, params)
        if max_grad_norm is not None:
            grads, grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        grads = list(zip(grads, params))
        trainer = tf.train.RMSPropOptimizer(learning_rate=LR, decay=alpha, epsilon=epsilon)
        _train = trainer.apply_gradients(grads)

        lr = Scheduler(v=lr, nvalues=total_timesteps, schedule=lrschedule)

        def train(obs, states, rewards, masks, actions, values, qvalues):
            advs = qvalues - values
            for step in range(len(obs)):
                cur_lr = lr.value()
            td_map = {train_model.X:obs, A:actions, ADV:advs, R:rewards, LR:cur_lr}
            if states != []:
                td_map[train_model.S] = states
                td_map[train_model.M] = masks
            policy_loss, value_loss, policy_entropy, _ = sess.run(
                [pg_loss, vf_loss, entropy, _train],
                td_map
            )
            return policy_loss, value_loss, policy_entropy

        def save():
            modelfile = os.path.join(logger.get_dir(), datetime.datetime.now().strftime("model-%Y-%m-%d-%H-%M-%S-%f"))

            ps = sess.run(params)
            joblib.dump(ps, modelfile)
            logger.log('Model saved to %s'%modelfile)

            model_files = sorted(fnmatch.filter(os.listdir(logger.get_dir()), "model-*"))
            if len(model_files) > nModelsToKeep:
                for old_file in model_files[0:-nModelsToKeep]:
                    os.remove(os.path.join(logger.get_dir(), old_file))

        def load(load_path):
            loaded_params = joblib.load(load_path)
            restores = []
            for p, loaded_p in zip(params, loaded_params):
                restores.append(p.assign(loaded_p))
            ps = sess.run(restores)
            logger.log('Model loaded from %s'%load_path)

        self.train = train
        self.train_model = train_model
        self.step_model = step_model
        self.step = step_model.step
        self.value = step_model.value
        self.initial_state = step_model.initial_state
        self.save = save
        self.load = load
        tf.global_variables_initializer().run(session=sess)

class Runner(object):

    def __init__(self, env, model, nsteps=5, nstack=4, gamma=0.99):
        self.env = env
        self.model = model
        nh, nw, nc = env.observation_space.shape
        nenv = env.num_envs
        self.nEnvs = nenv
        self.batch_ob_shape = (nenv*nsteps, nh, nw, nc*nstack)
        self.nc = nc
        self.gamma = gamma
        self.nsteps = nsteps
        def reset():
            self.obs = np.zeros((nenv, nh, nw, nc*nstack), dtype=np.uint8)
            obs = env.reset()
            self.update_obs(obs)
            self.states = model.initial_state
            self.dones = [False for _ in range(nenv)]
        self.reset = reset
        self.reset()

    def update_obs(self, obs):
        # Do frame-stacking here instead of the FrameStack wrapper to reduce
        # IPC overhead
        self.obs = np.roll(self.obs, shift=-self.nc, axis=3)
        self.obs[:, :, :, -self.nc:] = obs

    def run(self):
        mb_obs, mb_rewards, mb_actions, mb_values, mb_qvalues, mb_dones = [],[],[],[],[],[]
        mb_states = self.states
        for n in range(self.nsteps):
            actions, values, qvalues, states = self.model.step(self.obs, self.states, self.dones)
            # save only the acted Q-values
            qvalues = qvalues[np.arange(len(qvalues)), actions]
            mb_obs.append(np.copy(self.obs))
            mb_actions.append(actions)
            mb_qvalues.append(qvalues)
            mb_values.append(values)
            mb_dones.append(self.dones)
            obs, rewards, dones, _ = self.env.step(actions)
            self.states = states
            self.dones = dones
            for n, done in enumerate(dones):
                if done:
                    self.obs[n] = self.obs[n]*0
            self.update_obs(obs)
            mb_rewards.append(rewards)
        mb_dones.append(self.dones)
        #batch of steps to batch of rollouts
        mb_obs = np.asarray(mb_obs, dtype=np.uint8).swapaxes(1, 0).reshape(self.batch_ob_shape)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32).swapaxes(1, 0)
        mb_actions = np.asarray(mb_actions, dtype=np.int32).swapaxes(1, 0)
        mb_qvalues = np.asarray(mb_qvalues, dtype=np.float32).swapaxes(1, 0)
        mb_values = np.asarray(mb_values, dtype=np.float32).swapaxes(1, 0)
        mb_dones = np.asarray(mb_dones, dtype=np.bool).swapaxes(1, 0)
        mb_masks = mb_dones[:, :-1]
        mb_dones = mb_dones[:, 1:]
        last_values = self.model.value(self.obs, self.states, self.dones).tolist()
        #discount/bootstrap off value fn
        for n, (rewards, dones, value) in enumerate(zip(mb_rewards, mb_dones, last_values)):
            rewards = rewards.tolist()
            dones = dones.tolist()
            if dones[-1] == 0:
                rewards = discount_with_dones(rewards+[value], dones+[0], self.gamma)[:-1]
            else:
                rewards = discount_with_dones(rewards, dones, self.gamma)
            mb_rewards[n] = rewards
        mb_rewards = mb_rewards.flatten()
        mb_actions = mb_actions.flatten()
        mb_qvalues = mb_qvalues.flatten()
        mb_values = mb_values.flatten()
        mb_masks = mb_masks.flatten()
        return mb_obs, mb_states, mb_rewards, mb_masks, mb_actions, mb_values, mb_qvalues

    def eval(self, maxStepsPerEpisode=4500, nEpisodes=50):
        total_rewards, total_dones = [],[]
        total_steps, total_episodes = 0, 0

        # Run for maxStepsPerEpisode steps, or nEpisodes episodes, whichever is shorter
        while total_steps < maxStepsPerEpisode and total_episodes < nEpisodes:
            self.reset()
            step_rewards, step_dones = [],[]
            ep_dones = np.asarray(self.dones, dtype=np.bool)

            for n in range(int(maxStepsPerEpisode)):
                actions, _, _, states = self.model.step(self.obs, self.states, self.dones)
                step_dones.append(self.dones)
                obs, rewards, dones, _ = self.env.step(actions)
                self.states = states
                self.dones = dones
                for i, done in enumerate(dones):
                    if done:
                        self.obs[i] = self.obs[i]*0 # ignore observation
                self.update_obs(obs)
                step_rewards.append(rewards)
                ep_dones = np.logical_or(ep_dones, np.asarray(dones, dtype=np.bool))
                if np.all(ep_dones):
                    break
            ep_steps = n+1
            step_dones.append(self.dones)

            #batch of steps to batch of rollouts
            env_rewards = np.asarray(step_rewards, dtype=np.float32).swapaxes(1, 0)
            env_dones = np.asarray(step_dones, dtype=np.bool).swapaxes(1, 0)
            env_dones = env_dones[:, 1:]

            ep_dones, ep_rewards = [], []
            # compute total scores
            for n, (rewards, dones) in enumerate(zip(env_rewards, env_dones)):
                rewards = rewards.tolist()
                dones = dones.tolist()
                rawscore = discount_with_dones(rewards, dones, gamma=1.0)[0]
                done = (True in dones)
                ep_rewards.append(rawscore)
                ep_dones.append(done)

            total_rewards.append(ep_rewards)
            total_dones.append(ep_dones)
            total_steps += ep_steps
            total_episodes += self.nEnvs

        total_rewards = np.asarray(total_rewards, dtype=np.float32)
        total_dones = np.asarray(total_dones, dtype=np.bool)
        total_rewards = total_rewards.flatten()
        total_dones = total_dones.flatten()

        avg_score = np.mean(total_rewards)
        n_timeouts = np.size(total_dones) - np.sum(total_dones)

        logger.log("Evaluation complete:")
        logger.record_tabular("eval_avg_score", avg_score)
        logger.record_tabular("eval_steps", total_steps*self.nEnvs)
        logger.record_tabular("eval_episodes", total_episodes)
        logger.record_tabular("eval_n_timeouts", n_timeouts)
        logger.dump_tabular()

def learn(policy, env, eval_env, seed, nsteps=5, nstack=4, total_timesteps=int(80e6),
    pg_coef=1.0, vf_coef=0.5, ent_coef=0.01, max_grad_norm=0.5, lr=7e-4, lrschedule='linear',
    epsilon=1e-5, alpha=0.99, gamma=0.99, log_interval=100, eval_interval=12500, model_path=""):
    tf.reset_default_graph()
    set_global_seeds(seed)

    nenvs = env.num_envs
    ob_space = env.observation_space
    ac_space = env.action_space
    num_procs = len(env.remotes) # HACK
    model = Model(policy=policy, ob_space=ob_space, ac_space=ac_space, nenvs=nenvs, nsteps=nsteps, nstack=nstack, num_procs=num_procs, pg_coef=pg_coef, ent_coef=ent_coef, vf_coef=vf_coef,
        max_grad_norm=max_grad_norm, lr=lr, alpha=alpha, epsilon=epsilon, total_timesteps=total_timesteps, lrschedule=lrschedule)
    if model_path:
        model.load(model_path)
    runner = Runner(env, model, nsteps=nsteps, nstack=nstack, gamma=gamma)
    eval_runner = Runner(eval_env, model, nsteps=1, nstack=nstack, gamma=gamma)

    nbatch = nenvs*nsteps
    tstart = time.time()
    for update in range(1, total_timesteps//nbatch+1):
        obs, states, rewards, masks, actions, values, qvalues = runner.run()
        policy_loss, value_loss, policy_entropy = model.train(obs, states, rewards, masks, actions, values, qvalues)
        nseconds = time.time()-tstart
        fps = int((update*nbatch)/nseconds)
        if update % log_interval == 0 or update == 1:
            ev = explained_variance(qvalues, rewards)
            logger.record_tabular("nupdates", update)
            logger.record_tabular("total_timesteps", update*nbatch)
            logger.record_tabular("fps", fps)
            logger.record_tabular("policy_entropy", float(policy_entropy))
            logger.record_tabular("value_loss", float(value_loss))
            logger.record_tabular("explained_variance", float(ev))
            logger.dump_tabular()
        if update % eval_interval == 0 or update == 1:
            logger.record_tabular("nupdates", update)
            logger.record_tabular("total_timesteps", update*nbatch)
            eval_runner.eval()
            model.save()

    logger.record_tabular("nupdates", update)
    logger.record_tabular("total_timesteps", update*nbatch)
    eval_runner.eval()
    model.save()

if __name__ == '__main__':
    main()
