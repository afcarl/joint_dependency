from __future__ import division

from joint_dependency.simulation import (create_world, create_lockbox,
                                         Controller,
                                         ActionMachine)
from joint_dependency.recorder import Record
from joint_dependency.inference import (model_posterior, model_posterior_pairs, same_segment,
                                        exp_cross_entropy, exp_cross_entropy_with_pairs, random_objective,
                                        exp_neg_entropy, heuristic_proximity)

try:
    from joint_dependency.ros_adapter import (RosActionMachine,
                                              create_ros_lockbox)
except ImportError:
    print("Disable ROS.")

from joint_dependency.utils import rand_max

try:
    import bayesian_changepoint_detection.offline_changepoint_detection as bcd
except:
    bcd = None
    print("Disable Changepoint Detection")

from functools import partial
import datetime

try:
    import dill as cPickle
except ImportError:
    import pickle as cPickle
import multiprocessing
import multiprocessing.dummy
import argparse

import numpy as np
import pandas as pd
from scipy.stats import entropy

from progressbar import ProgressBar, Bar, Percentage
from blessings import Terminal

from copy import deepcopy
import time

import operator as op

import matplotlib.pyplot as mlp

term = Terminal()

class Writer(object):
    """Create an object with a write method that writes to a
    specific place on the screen, defined at instantiation.

    This is the glue between blessings and progressbar.
    """

    def __init__(self, location):
        """
        Input: location - tuple of ints (x, y), the position
                        of the bar in the terminal
        """
        self.location = location

    def write(self, string):
        with term.location(*self.location):
            print(string)


def generate_filename(metadata):
    return "data_" + str(metadata["Date"]).replace(" ", "-") \
        .replace("/", "-").replace(":", "-") + "_" + metadata['Objective'] + (".pkl")


def init(world):
    P_cp = []
    experiences = []
    for i, joint in enumerate(world.joints):
        P_cp.append(np.array([.1] * 360))
        experiences.append([])
    return P_cp, experiences


## Compute the probability of being in the same segment
def compute_p_same(p_cp):
    p_same = []
    for pcp in p_cp:
        p_same.append(same_segment(pcp))
    return p_same


def get_best_point(objective_fnc, experiences, p_same, alpha_prior,
                   model_prior, N_samples, world, locked_states,
                   action_sampling_fnc,
                   idx_last_successes=[], idx_last_failures=[],
                   use_joint_positions=False):
    actions = action_sampling_fnc(N_samples, world, locked_states)

    action_values = []
    for action in actions:
        check_joint = np.random.randint(0, len(world.joints))
        value = objective_fnc(experiences[check_joint],
                              action[1],
                              np.asarray(p_same),
                              alpha_prior,
                              model_prior[check_joint],
                              None,
                              idx_last_successes,
                              action[0],
                              idx_last_failures,
                              world,
                              use_joint_positions)
        action_values.append((action[1], check_joint, action[0], value))

    best_action = rand_max(action_values, lambda x: x[3])

    return best_action

def small_joint_state_sampling(_, world, locked_states):
    actions = []
    for j, joint in enumerate(world.joints):
        # if locked_states[j] == 0:
        for _pos in (joint.min_limit, joint.max_limit):
            pos = [joint.get_q() for joint in world.joints]
            if abs(pos[j] - _pos) < 0.9:
                continue
            pos[j] = _pos
            # TODO deepcopy needed?
            actions.append((j, deepcopy(pos)))
    return actions


def large_joint_state_sampling(N_samples, world, locked_states):
    actions = []
    for i in range(N_samples):
        pos = np.ndarray((len(world.joints),))
        for j, joint in enumerate(world.joints):
            if locked_states[j] == 1:
                pos[j] = int(joint.get_q())
            else:
                pos[j] = np.random.randint(joint.min_limit, joint.max_limit)
        actions.append((j, deepcopy(pos)))
    return actions


def large_joint_state_one_joint_moving_sampling(N_samples, world,
                                                locked_state):
    actions = []
    for i in range(N_samples):
        pos = np.asarray([int(joint.get_q()) for joint in world.joints])
        joint_idx = np.random.choice(
            np.where(np.asarray(locked_state) == 0)[0])
        joint = world.joints[joint_idx]
        pos[joint_idx] = np.random.randint(joint.min_limit, joint.max_limit)
        actions.append((joint_idx, deepcopy(pos)))
        # print((joint_idx, pos))
    return actions


def get_probability_over_degree(P, qs):
    probs = np.zeros((360,))
    count = np.zeros((360,))
    for i, pos in enumerate(qs[:-2]):
        deg = int(pos) % 360

        probs[deg] += P[i]
        count[deg] += 1

    probs = probs / count
    prior = 10e-8
    probs = np.array([prior if np.isnan(p) else p for p in probs])
    return probs, count


def update_p_cp(world, use_ros):
    P_cp = []
    pid = multiprocessing.current_process().pid
    for j, joint in enumerate(world.joints):
        if use_ros:
            q = Record.records[pid]["q_" + str(j)].as_matrix()
            af = Record.records[pid]["applied_force_" + str(j)][0:].as_matrix()
            v = q[1:] - q[0:-1]  # we can't measure the velocity directly

            vn = v[:] + af[1:]
            d = np.zeros((v.shape[0] + 1,))
            d[1:] = abs((vn ** 2 - v[:] ** 2) / (0.1 * vn))
        else:
            v = Record.records[pid]["v_" + str(j)][0:].as_matrix()
            af = Record.records[pid]["applied_force_" + str(j)][0:].as_matrix()

            vn = v[:-1] + af[:-1]
            d = np.zeros(v.shape)
            d[1:] = abs((vn ** 2 - v[1:] ** 2) / (0.1 * vn))

        nans, x = nan_helper(d)
        d[nans] = np.interp(x(nans), x(~nans), d[~nans])

        Q, P, Pcp = bcd.offline_changepoint_detection(
            data=d,
            prior_func=partial(bcd.const_prior, l=(len(d) + 1)),
            observation_log_likelihood_function=
            bcd.gaussian_obs_log_likelihood,
            truncate=-50)

        p_cp, count = get_probability_over_degree(
            np.exp(Pcp).sum(0)[:1],
            Record.records[pid]['q_' + str(j)][-1:].as_matrix())

        P_cp.append(p_cp)
    return P_cp


def nan_helper(y):
    """Helper to handle indices and logical indices of NaNs.

    Input:
        - y, 1d numpy array with possible NaNs
    Output:
        - nans, logical indices of NaNs
        - index, a function, with signature indices= index(logical_indices),
          to convert logical indices of NaNs to 'equivalent' indices
    Example:
        >>> # linear interpolation of NaNs
        >>> nans, x= nan_helper(y)
        >>> y[nans]= np.interp(x(nans), x(~nans), y[~nans])
    """

    return np.isnan(y), lambda z: z.nonzero()[0]

"""Calculate posterior probabilities for the dependency structure

Input:
    - world
    - experiences
    - P_same
    - alpha_prior
    - model_prior
Output:
    - posteriors
"""
def calc_posteriors(world, experiences, P_same, alpha_prior, model_prior):
    posteriors = []
    for i, joint in enumerate(world.joints):
        posteriors.append(model_posterior(experiences[i], np.asarray(P_same),
                                          alpha_prior,
                                          np.asarray(model_prior[i])))
    return posteriors

def calc_posteriors_with_pairs(world, experiences, P_same, alpha_prior, model_prior):
    posteriors = []
    for i, joint in enumerate(world.joints):
        posteriors.append(model_posterior_pairs(experiences[i], np.asarray(P_same),
                                          alpha_prior,
                                          np.asarray(model_prior[i]), model_prior.shape[0]))
    return posteriors

def dependency_learning(N_actions, N_samples, world, objective_fnc,
                        use_change_points, alpha_prior, model_prior,
                        action_machine, location, action_sampling_fnc,
                        use_ros, use_joint_positions=False):
    # writer = Writer(location)
    widgets = [Bar(), Percentage(),
               " (Run #{}, PID {})".format(0,
                                           multiprocessing.current_process().pid)]
    progress = ProgressBar(maxval=N_actions + 2,  # fd=writer,
                           widgets=widgets).start()
    progress.update(0)
    # init phase
    # initialize the probability distributions
    P_cp, experiences = init(world)

    # get locking state of all joints by actuating them once
    jpos = np.array([int(j.get_q()) for j in world.joints])
    locked_states = [None] * len(world.joints)
    locked_states_before = [None] * len(world.joints)

    if use_change_points:
        for i, joint in enumerate(world.joints):
            print(action_machine)
            action_pos = np.array(jpos)
            action_pos[i] = world.joints[i].max_limit
            action_machine.run_action(action_pos)
            action_pos[i] = world.joints[i].min_limit
            action_machine.run_action(action_pos)
        P_cp = update_p_cp(world, use_ros)
        P_same = compute_p_same(P_cp)
    else:
        P_same = compute_p_same(P_cp)

        # for j, joint in enumerate(world.joints):
        #     locked_states[j] = action_machine.check_state(j)

        # add the experiences
        # new_experience = {'data': jpos, 'value': locked_states[j]}
        # experiences[j].append(new_experience)

    print P_same[0]

    #mlp.matshow(P_same[0], interpolation='nearest')
    #mlp.show()

    # perform actions as long the entropy of all model distributions is still
    # big
    # while (np.array([entropy(p) for p in posteriors]) > .25).any():
    data = pd.DataFrame()

    progress.update(1)

    metadata = {'ChangePointDetection': use_change_points,
                'Date': datetime.datetime.now(),
                'Objective': objective_fnc.__name__,
                # 'World': world,
                'ModelPrior': model_prior,
                'AlphaPrior': alpha_prior,
                'P_cp': P_cp,
                'P_same': P_same,
                'DependencyGT' : world.dependency_structure_gt}

    idx_last_successes = []
    idx_last_failures = []

    # store empty data frame so file is available
    filename = generate_filename(metadata)
    with open(filename, "w") as _file:
        cPickle.dump((data, metadata), _file)

    # initial_data = pd.DataFrame(index=[-1])
    # for n, p in enumerate([-1,-1,-1,-1,-1]):
    #     initial_data["DesiredPos" + str(n)] = [p]
    # initial_data["CheckedJoint"] = [-1]
    #
    # jpos_before = np.array([int(j.get_q()) for j in world.joints])
    # for n, p in enumerate(jpos_before):
    #     initial_data["RealPos" + str(n)] = [p]
    #
    # for n, joint in enumerate(world.joints):
    #     initial_data["LSAfterAction" + str(n)] = [joint.is_locked()]
    #
    # for n, p in enumerate([1,1,1,1,1]):
    #     initial_data["Posterior" + str(n)] = [p]
    #     initial_data["Entropy" + str(n)] = [entropy(p)]
    # data = data.append(initial_data)

    for idx in range(N_actions):
        current_data = pd.DataFrame(index=[idx])

        lock_states_before = [joint.is_locked() for joint in world.joints]
        for n, p in enumerate(lock_states_before):
            current_data["LSBefore" + str(n)] = [p]

        # get best action according to objective function
        desired_joint_configurations, checked_joint, desired_joint_to_move, value = \
            get_best_point(objective_fnc,
                           experiences,
                           P_same,
                           alpha_prior,
                           model_prior,
                           N_samples,
                           world,
                           locked_states,
                           action_sampling_fnc,
                           idx_last_successes,
                           idx_last_failures,
                           use_joint_positions)

        if desired_joint_to_move is None:
            print("We finished the exploration")
            print("This usually happens when you use the heuristic_proximity "
                  "that has as objective to estimate the dependency structure "
                  "and not to reduce the entropy")
            break

        for n, p in enumerate(desired_joint_configurations):
            current_data["DesiredPos" + str(n)] = [p]
        current_data["DesJToMove"] = [desired_joint_to_move]

        # save the joint and locked states before the action
        locked_states_before = [joint.is_locked()
                                for joint in world.joints]
        jpos_before = np.array([int(j.get_q()) for j in world.joints])

        for n, p in enumerate(jpos_before):
            current_data["RealPosBef" + str(n)] = [p]

        desired_joint_to_move_was_unlocked = True
        if np.all(np.abs(desired_joint_configurations - jpos_before) < .1):
            # if we want a no-op don't actually call the robot
            jpos = desired_joint_configurations

        else:
            # run best action, i.e. move joints to desired position
            desired_joint_to_move_was_unlocked = action_machine.run_action(desired_joint_configurations, desired_joint_to_move)

            # get real position after action (PD-controllers aren't perfect)
            final_joint_configurations = np.array([int(j.get_q()) for j in world.joints])

        for n, p in enumerate(final_joint_configurations):
            current_data["RealPos" + str(n)] = [p]

        # save the locked states after the action
        # test whether the joints are locked or not
        ##################################################
        locked_states = [joint.is_locked()
                         for joint in world.joints]


        #measured_locked_state = locked_states[desired_joint_to_move]
        measured_locked_state = not desired_joint_to_move_was_unlocked

        # Check that 'not desired_joint_to_move_was_unlocked' is always == locked_states[desired_joint_to_move]
        if locked_states[desired_joint_to_move] != (not desired_joint_to_move_was_unlocked):
            print "Error: locked_states[desired_joint_to_move] != (not desired_joint_to_move_was_unlocked)"
            exit(-1)


        for n, p in enumerate(locked_states):
            current_data["LSAfter" + str(n)] = [p]

        # if the locked states changed the action was successful, if not,
        # it was a failure
        # CORRECTION: it could be that a joint moves but it does not unlock a
        # mechanism. Then it won't be a failure nor a success. We just do not
        # add it no any list
        if desired_joint_to_move_was_unlocked:
            idx_last_failures = []
            idx_last_successes.append(desired_joint_to_move)
        else:
            idx_last_failures.append(desired_joint_to_move)

        # add new experience
        new_experience = {'data': final_joint_configurations, 'value': measured_locked_state}
        #print new_experience
        experiences[desired_joint_to_move].append(new_experience)

        #print "Experiences of joint ", desired_joint_to_move
        #print experiences[desired_joint_to_move]

        # calculate model posterior
        posteriors = calc_posteriors_with_pairs(world, experiences, P_same, alpha_prior,
                                     model_prior)
        for n, p in enumerate(posteriors):
            current_data["Posterior" + str(n)] = [p]
            current_data["Entropy" + str(n)] = [entropy(p)]


        #NEW: Compare the posterior and the ground truth for the dependency structure:
        #print 'Posterior'
        #print np.array(posteriors)
        #print 'GT'
        #print world.dependency_structure_gt

        kls = []
        for n, (pr, gtr) in enumerate(zip(np.array(posteriors), world.dependency_structure_gt)):
            kld = entropy(gtr, pr)
            kls += [kld]
            current_data["KLD" + str(n)] = kld

        print 'Kullback-Leibler divergences'
        print kls
        data = data.append(current_data)
        progress.update(idx + 1)

        filename = generate_filename(metadata)
        with open(filename, "w") as _file:
            cPickle.dump((data, metadata), _file)

    progress.finish()
    return data, metadata


def build_model_prior_simple(world, independent_prior):
    n = len(world.joints)

    # the model prior is proportional to 1/distance between the joints
    model_prior = np.array([[0 if x == y
                             else independent_prior if x == n
                             else 1 / abs(x - y)
                             for x in range(n + 1)]
                             for y in range(n)])

    # normalize
    model_prior[:, :-1] = ((model_prior.T[:-1, :] /
                            np.sum(model_prior[:, :-1], 1)).T *
                           (1 - independent_prior))

    return model_prior


def build_model_prior_3d(world, independent_prior):
    j = world.joints
    n = len(j)

    model_prior = np.array([[0 if x == y
                             else independent_prior
    if x == n
    else 1 / np.linalg.norm(
        np.asarray(j[x].position) - np.asarray(j[y].position)
    )
                             for x in range(n + 1)]
                            for y in range(n)])
    # normalize
    model_prior[:, :-1] = ((model_prior.T[:-1, :] /
                            np.sum(model_prior[:, :-1], 1)).T *
                           (1 - independent_prior))
    return model_prior

def ncr(n, r):
    r = min(r, n-r)
    if r == 0: return 1
    numer = reduce(op.mul, xrange(n, n-r, -1))
    denom = reduce(op.mul, xrange(1, r+1))
    return numer//denom

def build_model_prior_3d_with_pairs(world, independent_prior):
    j = world.joints
    n = len(j)

    num_models = n + ncr(n,2) + 1
    #Rows is the number of joints, columns is the number of models
    model_prior = np.zeros((n, num_models))

    for x in range(n):
        for y in range(len(model_prior[x])):
            if x == y:
                model_prior[x,y] = 0
            elif y == num_models-1:
                model_prior[x,y] = independent_prior
            elif y < n:
                model_prior[x,y] = 1 / np.linalg.norm(np.asarray(j[x].position) - np.asarray(j[y].position))
            else:
                model_prior[x,y] = 0.1

    # normalize
    model_prior[:, :-1] = ((model_prior.T[:-1, :] /
                            np.sum(model_prior[:, :-1], 1)).T *
                           (1 - independent_prior))
    return model_prior


def run_experiment(args):
    # reset all things for every new experiment
    pid = multiprocessing.current_process().pid
    seed = time.gmtime()
    np.random.seed(seed)
    if bcd:
        bcd.offline_changepoint_detection.data = None
    Record.records[pid] = pd.DataFrame()

    if args.use_ros:
        world = create_ros_lockbox()
        action_machine = RosActionMachine(world)
    else:
        world = create_lockbox(
            use_joint_positions=args.use_joint_positions,
            use_simple_locking_state=args.use_simple_locking_state,
            lockboxfile=args.lockboxfile)
        controllers = []
        for j, _ in enumerate(world.joints):
            controllers.append(Controller(world, j))
        action_machine = ActionMachine(world, controllers, .1)

    alpha_prior = np.array([.1, .1])

    independent_prior = .7

    # the model prior is proportional to 1/distance between the joints
    # if args.use_joint_positions:
    #model_prior = build_model_prior_3d(world, independent_prior)

    model_prior = build_model_prior_3d_with_pairs(world, independent_prior)

    print 'model_prior'
    print model_prior

    initial_entropy = [entropy(p) for p in model_prior]
    print 'Initial entropy'
    print initial_entropy
    # else:
    #     model_prior = build_model_prior_simple(world, independent_prior)

    # normalize
    # model_prior[:, :-1] = ((model_prior.T[:-1, :] /
    #                         np.sum(model_prior[:, :-1], 1)).T *
    #                        (1-independent_prior))

    if args.objective == "random":
        objective = random_objective
    elif args.objective == "entropy":
        objective = exp_neg_entropy
    elif args.objective == "cross_entropy":
        objective = exp_cross_entropy_with_pairs
    elif args.objective == "heuristic_proximity":
        objective = heuristic_proximity
    else:
        raise Exception("You tried to choose an objective that doesn't exist: " + args.objective)

    if args.joint_state == "small":
        action_sampling_fnc = small_joint_state_sampling
    elif args.joint_state == "large":
        action_sampling_fnc = large_joint_state_one_joint_moving_sampling
    else:
        raise Exception("No proper action sampling function chosen.")

    data, metadata = dependency_learning(
        N_actions=args.queries,
        N_samples=args.samples,
        world=world,
        objective_fnc=objective,
        use_change_points=args.changepoint,
        alpha_prior=alpha_prior,
        model_prior=model_prior,
        action_machine=action_machine,
        location=None,
        action_sampling_fnc=action_sampling_fnc,
        use_ros=args.use_ros,
        use_joint_positions=args.use_joint_positions)

    metadata['Seed'] = seed
    filename = generate_filename(metadata)
    with open(filename, "wb") as _file:
        cPickle.dump((data, metadata), _file)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--objective", required=True,
                        help="The objective to optimize for exploration",
                        choices=['random', 'entropy', 'cross_entropy',
                                 'heuristic_proximity'])
    parser.add_argument("-c", "--changepoint", action='store_true',
                        help="Should change points used as prior")
    parser.add_argument("-t", "--threads", type=int,
                        default=multiprocessing.cpu_count(),
                        help="Number of threads used")
    parser.add_argument("-q", "--queries", type=int, default=20,
                        help="How many queries should the active learner make")
    parser.add_argument("-s", "--samples", type=int, default=4000,
                        help="How many samples should be drawn for "
                             "optimization")
    parser.add_argument("-r", "--runs", type=int, default=20,
                        help="Number of runs")
    parser.add_argument("-p", "--prob-file", type=str, default=None,
                        help="The file with the probability distributions")
    parser.add_argument("--use_ros", action='store_true',
                        help="Enable ROS/real robot usage.")
    parser.add_argument("--joint_state", type=str, default='large',
                        help="Should we use a large or a small joint state "
                             "(large/small).")
    parser.add_argument("--use_joint_positions", action='store_true',
                        help="Don't assume a linear sequence of joints but 3d "
                             "positions.")
    parser.add_argument("--use_simple_locking_state", action='store_true',
                        help="Don't randomize the locking configuration, but "
                             "have joint limits lock other joints")
    parser.add_argument("-l", "--lockboxfile", type=str, default=None,
                        help="A file that contains a lock-box specification")

    args = parser.parse_args()

    #print(term.clear)

    run_experiment(args)

    #print(term.clear)


if __name__ == '__main__':
    main()
