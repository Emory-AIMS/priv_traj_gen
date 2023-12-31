import argparse
import random
import numpy as np
import torch
import tqdm
from torch import nn, optim
import json
from scipy.spatial.distance import jensenshannon
from collections import Counter
import pathlib

from name_config import make_model_name, make_save_name
from my_utils import get_datadir, privtree_clustering, depth_clustering, noise_normalize, add_noise, plot_density, make_trajectories, set_logger, construct_default_quadtree, save, load, compute_num_params, set_budget
from dataset import TrajectoryDataset
from models import compute_loss_meta_gru_net, compute_loss_gru_meta_gru_net, Markov1Generator, MetaGRUNet, MetaNetwork, FullLinearQuadTreeNetwork, guide_to_model
import torch.nn.functional as F
from opacus.utils.batch_memory_manager import BatchMemoryManager

from opacus import PrivacyEngine
from pytorchtools import EarlyStopping
import evaluation



def train_meta_network(meta_network, next_location_counts, n_iter, early_stopping, distribution="dirichlet"):
    device = next(iter(meta_network.parameters())).device
    optimizer = optim.Adam(meta_network.parameters(), lr=0.001)
    n_classes = meta_network.n_classes
    n_locations = len(next_location_counts[0])
    batch_size = 100
    epoch = 0
    n_bins = int(np.sqrt(n_locations)) -2
    tree = construct_default_quadtree(n_bins)
    tree.make_self_complete()

    def depth_to_ids(depth):
        return [node.id for node in tree.get_nodes(depth)]
    # make test data
    # test_input = torch.eye(n_classes).to(device)

    original_targets = torch.zeros(n_classes, n_locations).to(device)
    for i in range(n_classes):
        original_targets[i] = torch.tensor(next_location_counts[i])
        original_targets[i][original_targets[i] < 0] = 0
        original_targets[i] = original_targets[i] / original_targets[i].sum()

    if distribution == "eye":
        input = torch.eye(n_classes).to(device)

        # normalize
        input = input / input.sum(dim=1).reshape(-1,1)
        # target is the distribution generated by sum of next_location_distributions weighted by input
        target = torch.zeros(input.shape[0], n_locations).to(device)
        for i in range(n_classes):
            target += input[:,i].reshape(-1,1) * next_location_counts[i]
        # normalize target
        target[target < 0] = 0
        target = target / target.sum(dim=1).reshape(-1,1)
        target = tree.make_quad_distribution(target)

    with tqdm.tqdm(range(n_iter)) as pbar:
        for epoch in pbar:
            # make input: (batch_size, n_classes)
            # input is sampled from Dirichlet distribution
            if distribution == "dirichlet":
                input = torch.distributions.dirichlet.Dirichlet(torch.ones(n_classes)).sample((batch_size,)).to(device)

                # normalize
                # input = input / input.sum(dim=1).reshape(-1,1)
                # target is the distribution generated by sum of next_location_distributions weighted by input
                target = torch.zeros(input.shape[0], n_locations).to(device)
                for i in range(n_classes):
                    target += input[:,i].reshape(-1,1) * next_location_counts[i]
                # normalize target
                target[target < 0] = 0
                target = target / target.sum(dim=1).reshape(-1,1)
            elif distribution == "eye":
                input = torch.eye(n_classes).to(device)
            elif distribution == "both":
                input = torch.distributions.dirichlet.Dirichlet(torch.ones(n_classes)).sample((batch_size,)).to(device)
                input = torch.cat([input, torch.eye(n_classes).to(device)], dim=0)
                target = torch.zeros(input.shape[0], n_locations).to(device)
                for i in range(n_classes):
                    target += input[:,i].reshape(-1,1) * next_location_counts[i]
                target[target < 0] = 0
                target = target / target.sum(dim=1).reshape(-1,1)
            else:
                raise NotImplementedError
            
            losses = []
            loss = 0

            meta_network_output = meta_network(input)
            if type(meta_network_output) == list:
                batch_size = meta_network_output[0].shape[0]
                test_target = evaluation.make_target_distributions_of_all_layers(target, tree)
                train_all_layers = True
                if train_all_layers:
                    # meta_network_output = meta_network.to_location_distribution(meta_network_output, target_depth=0)
                    for depth in range(tree.max_depth):
                        losses.append(F.kl_div(meta_network_output[depth].view(batch_size,-1), test_target[depth], reduction='batchmean'))
                else:
                    meta_network_output = meta_network.to_location_distribution(meta_network_output, target_depth=-1)
                    losses.append(F.kl_div(meta_network_output.view(batch_size,-1), test_target[-1], reduction='batchmean'))
                loss = sum(losses)
            else:
                quad_loss = args.network_type == "hiemrnet"
                if quad_loss:
                    if distribution != "eye":
                        target = tree.make_quad_distribution(target)
                    meta_network_output = meta_network_output.view(*target.shape)
                    for depth in range(tree.max_depth):
                        ids = depth_to_ids(depth)
                        losses.append(F.kl_div(meta_network_output[:,ids,:], target[:,ids,:], reduction='batchmean') * 4**(tree.max_depth-depth-1))
                else:
                    meta_network_output = meta_network_output.view(*target.shape)
                    losses.append(F.kl_div(meta_network_output, target, reduction='batchmean'))
            # loss = compute_loss_meta_quad_tree_attention_net(meta_network_output, target, meta_network.tree)
            loss = sum(losses)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()


            pbar.set_description(f"loss: {loss.item()} ({[v.item() for v in losses]})")
            early_stopping(loss.item(), meta_network)

            if early_stopping.early_stop:
                meta_network.load_state_dict(torch.load(save_dir / "meta_network.pt"))
                logger.info(f"load meta network from {save_dir / 'meta_network.pt'}")
                break

    logger.info(f"best loss of meta training at {epoch}: {early_stopping.best_score}")



def make_targets_of_all_layers(target_locations, tree):
    n_locations = len(tree.get_leafs())
    batch_size = target_locations.shape[0]
    target_locations = target_locations.view(-1)
    node_paths = [tree.state_to_node_path(state.item())[1:] for state in target_locations]
    output = []
    for node_path in node_paths:
        if None in node_path:
            target = [TrajectoryDataset.ignore_idx(n_locations) for _ in range(tree.max_depth)]
        else:
            target = [node.oned_coordinate for node in node_path]
        output.append(target)
    output_ = []
    for i in range(tree.max_depth):
        output_.append(torch.tensor([location[i] for location in output]).view(batch_size, -1).to(target_locations.device))
    return output_

def train_with_discrete_time(generator, optimizer, loss_model, input_locations, target_locations, input_times, target_times, labels, coef_location, coef_time, train_all_layers=False):
    is_dp = hasattr(generator, "module")
    if loss_model == compute_loss_gru_meta_gru_net:
        target_locations = torch.tensor([generator.meta_net.tree.state_to_path(state.item()) for state in target_locations.view(-1)]).view(target_locations.shape[0], target_locations.shape[1], generator.meta_net.tree.max_depth).to(target_locations.device)
        output_locations, output_times = generator([input_locations, input_times], labels, target=target_locations)
    else:
        output_locations, output_times = generator([input_locations, input_times], labels)
        if train_all_layers:
            target_locations = make_targets_of_all_layers(target_locations, generator.meta_net.tree)

    # if generator.meta_net.is_consistent:
    if False:
        # same thing as the below one
        losses = []
        for i in range(len(target_locations)):
            loss_depth_i = 0
            counter_depth_i = 0
            for j in range(len(target_locations[i])):
                for k in range(len(target_locations[i][j])):
                    if target_locations[i][j][k].item() != TrajectoryDataset.ignore_idx(generator.meta_net.n_locations):
                        # new_target_locations.append(0)
                        # new_output_locations.append(output_locations[i][j][k][target_locations[i][j][k]])
                        # print(output_locations[i][j][k][target_locations[i][j][k]].view(-1,1))
                        loss_depth_i += (torch.nn.functional.nll_loss(output_locations[i][j][k][target_locations[i][j][k]].view(-1,1), torch.tensor([0])))
                        counter_depth_i += 1
            losses.append(loss_depth_i / counter_depth_i)

        losses.append(F.nll_loss(output_times.view(-1, output_times.shape[-1]), (target_times).view(-1)))
    
    losses = loss_model(target_locations, target_times, output_locations, output_times, coef_location, coef_time)
    loss = sum(losses)
    optimizer.zero_grad()
    loss.backward()

    if is_dp:
        norms = []
        # get the norm of gradient example
        for name, param in generator.named_parameters():
            if 'grad_sample' not in vars(param):
                # in this case, the gradient is already accumulated
                # norms.append(param.grad.reshape(len(param.grad), -1).norm(2, dim=-1))
                pass
            else:
                norms.append(param.grad_sample.reshape(len(param.grad_sample), -1).norm(2, dim=-1))
        
        if len(norms[0]) > 1:
            norms = torch.stack(norms, dim=1).norm(2, dim=-1).detach().cpu().numpy()
        else:
            norms = torch.concat(norms, dim=0).detach().cpu().numpy()
    else:
        # compute the norm of gradient
        norms = []
        for name, param in generator.named_parameters():
            if param.grad is None:
                continue
            norms.append(param.grad.reshape(-1))
        norms = torch.cat(norms, dim=0)
        # print("are", norms.max(), norms.min())
        norm = norms.norm(2, dim=-1).detach().cpu().numpy()
        norms = [norm]

    optimizer.step()
    losses = [loss.item() for loss in losses]
    losses.append(np.mean(norms))

    return losses


def train_epoch(data_loader, generator, optimizer):
    losses = []
    device = next(generator.parameters()).device
    for i, batch in enumerate(data_loader):
        input_locations = batch["input"].to(device, non_blocking=True)
        target_locations = batch["target"].to(device, non_blocking=True)
        references = [tuple(v) for v in batch["reference"]]
        input_times = batch["time"].to(device, non_blocking=True)
        target_times = batch["time_target"].to(device, non_blocking=True)

        loss = train_with_discrete_time(generator, optimizer, loss_model, input_locations, target_locations, input_times, target_times, references, args.coef_location, args.coef_time, train_all_layers=args.train_all_layers)
        # print(norm)
        losses.append(loss)

    return np.mean(losses, axis=0)

def clustering(clustering_type, n_locations):
    n_bins = int(np.sqrt(n_locations)) -2
    if clustering_type == "distance":
        distance_matrix = np.load(training_data_dir.parent.parent / f"distance_matrix_bin{n_bins}.npy")
        location_to_class = evaluation.clustering(dataset.global_counts[0], distance_matrix, args.n_classes)
        privtree = None
    elif clustering_type == "privtree":
        location_to_class, privtree = evaluation.privtree_clustering(dataset.global_counts[0], theta=args.privtree_theta)
    elif clustering_type == "depth":
        location_to_class, privtree = depth_clustering(n_bins)
    else:
        raise NotImplementedError
    return location_to_class, privtree

def construct_meta_network(clustering_type, network_type, n_locations, memory_dim, memory_hidden_dim, location_embedding_dim, multilayer, consistent, logger):

    logger.info(f"clustering type: {clustering_type}")
    location_to_class, privtree = clustering(clustering_type, n_locations)
    # class needs to correspond to node 
    n_classes = len(set(location_to_class.values()))

    meta_network_class, _ = guide_to_model(network_type)
    if network_type == "markov1":
        # normalize count by dim = 1
        target_counts = target_counts / target_counts.sum(dim=1).reshape(-1,1)
        generator = Markov1Generator(target_counts.cpu(), location_to_class)
        eval_generator = generator
        optimizer = None
        data_loader = None
        privacy_engine = None
        args.n_epochs = 0
    else:

        if network_type == "baseline":
            meta_network = meta_network_class(memory_hidden_dim, memory_dim, n_locations, n_classes, "relu")
        elif network_type == "hiemrnet":
            meta_network = meta_network_class(n_locations, memory_dim, memory_hidden_dim, location_embedding_dim, privtree, "relu", multilayer=multilayer, is_consistent=consistent)
        compute_num_params(meta_network, logger)
        
    return meta_network, location_to_class

def pre_training_meta_network(meta_network, dataset, location_to_class, transition_type):
    if args.meta_n_iter == 0:
        pass
    
    else:
        

        n_classes = len(set(location_to_class.values()))
        target_counts = []
        for i in range(n_classes):
            if transition_type == "marginal":
                logger.info(f"use marginal transition matrix")
                next_location_counts = dataset.next_location_counts
            elif transition_type == "first":
                logger.info(f"use first transition matrix")
                next_location_counts = evaluation.make_next_location_count(dataset, 0)
            elif transition_type == "test":
                logger.info(f"use test transition matrix")
                next_location_counts = {location: [1] * dataset.n_locations for location in range(dataset.n_locations)}

            # find the locations belonging to the class i
            next_location_count_i = torch.zeros(dataset.n_locations)
            locations = [location for location, class_ in location_to_class.items() if class_ == i]
            logger.info(f"n locations in class {i}: {len(locations)}")
            for location in locations:
                if location in next_location_counts:
                    next_location_count_i += np.array(next_location_counts[location]) 
            logger.info(f"sum of next location counts in class {i}: {sum(next_location_count_i)} add noise by epsilon = {args.epsilon}")
            target_count_i = add_noise(next_location_count_i, args.global_clip, args.epsilon)
            target_count_i = torch.tensor(target_count_i)
            
            target_counts.append(target_count_i)

            plot_density(target_count_i, dataset.n_locations, save_dir / "imgs" / f"class_next_location_distribution_{i}.png")

        device = next(meta_network.parameters()).device
        target_counts = torch.stack(target_counts).to(device)
        if args.meta_network_load_path == "None":
            early_stopping = EarlyStopping(patience=args.meta_patience, path=save_dir / "meta_network.pt", delta=1e-6)
            train_meta_network(meta_network, target_counts, args.meta_n_iter, early_stopping, args.meta_dist)
            args.meta_network_load_path = str(save_dir / "meta_network.pt")
        else:
            meta_network.load_state_dict(torch.load(args.meta_network_load_path))
            logger.info(f"load meta network from {args.meta_network_load_path}")

        # plot the test output of meta_network
        with torch.no_grad():
            meta_network.pre_training = False
            meta_network.eval()
            test_input = torch.eye(n_classes).to(device)
            meta_network_output = meta_network(test_input)
            if type(meta_network_output) == list:
                meta_network_output = meta_network_output[-1]
            for i in range(n_classes):
                plot_density(torch.exp(meta_network_output[i]).cpu().view(-1), dataset.n_locations, save_dir / "imgs" / f"meta_network_output_{i}.png")
            meta_network.train()


    if hasattr(meta_network, "remove_class_to_query"):
        meta_network.remove_class_to_query()
    
    return meta_network

def construct_generator(n_locations, meta_network, network_type, location_embedding_dim, n_split, trajectory_type_dim, hidden_dim, reference_to_label, logger):

    _, generator_class = guide_to_model(network_type)

    # time_dim is n_time_split + 2 (because of the edges 0 and >max)
    generator = generator_class(meta_network, n_locations, location_embedding_dim, n_split+2, trajectory_type_dim, hidden_dim, reference_to_label)
    compute_num_params(generator, logger)
    
    return generator, compute_loss_meta_gru_net

def construct_dataset(training_data_path, route_data_path, n_time_split):

    print(training_data_path.parent)
    # load dataset config    
    with open(training_data_path.parent / "params.json", "r") as f:
        param = json.load(f)
    n_locations = param["n_locations"]
    dataset_name = param["dataset"]

    trajectories = load(training_data_path)
    if route_data_path is not None:
        try:
            route_trajectories = load(route_data_path)
        except:
            print("failed to load route data", route_data_path)
            route_trajectories = None        
    else:
        route_trajectories = None
    time_trajectories = load(training_data_path.parent / "training_data_time.csv")

    return TrajectoryDataset(trajectories, time_trajectories, n_locations, n_time_split, route_data=route_trajectories, dataset_name=dataset_name)


if __name__ == "__main__":
    # set argument
    parser = argparse.ArgumentParser()
    parser.add_argument('--cuda_number', type=int)
    parser.add_argument('--eval_interval', type=int)
    # parser.add_argument('--dataset', type=str)
    # parser.add_argument('--data_name', type=str)
    # parser.add_argument('--route_data_name', type=str)
    # parser.add_argument('--training_data_name', type=str)
    parser.add_argument('--training_data_dir', type=str)
    parser.add_argument('--network_type', type=str)
    parser.add_argument('--seed', type=int)
    parser.add_argument('--batch_size', type=int)
    parser.add_argument('--meta_n_iter', type=int)
    parser.add_argument('--n_epochs', type=int)
    parser.add_argument('--embed_dim', type=int)
    parser.add_argument('--hidden_dim', type=int)
    # parser.add_argument('--save_name', type=str)
    parser.add_argument('--accountant_mode', type=str)
    parser.add_argument('--meta_network_load_path', type=str)
    parser.add_argument('--transition_type', type=str)
    parser.add_argument('--meta_dist', type=str)
    parser.add_argument('--activate', type=str)
    parser.add_argument('--learning_rate', type=float)
    parser.add_argument('--dp_delta', type=float)
    parser.add_argument('--noise_multiplier', type=float)
    parser.add_argument('--clipping_bound', type=float)
    parser.add_argument('--epsilon', type=float)
    parser.add_argument('--n_split', type=int)
    parser.add_argument('--is_dp', action='store_true')
    parser.add_argument('--train_all_layers', action='store_true')
    parser.add_argument('--remove_first_value', action='store_true')
    parser.add_argument('--remove_duplicate', action='store_true')
    parser.add_argument('--consistent', action='store_true')
    parser.add_argument('--multilayer', action='store_true')
    parser.add_argument('--server', action='store_true')
    parser.add_argument('--patience', type=int)
    parser.add_argument('--physical_batch_size', type=int)
    parser.add_argument('--coef_location', type=float)
    parser.add_argument('--coef_time', type=float)
    parser.add_argument('--n_classes', type=int)
    parser.add_argument('--global_clip', type=int)
    parser.add_argument('--location_embedding_dim', type=int)
    parser.add_argument('--memory_dim', type=int)
    parser.add_argument('--memory_hidden_dim', type=int)
    parser.add_argument('--meta_patience', type=int)
    parser.add_argument('--privtree_theta', type=float)
    parser.add_argument('--clustering', type=str)
    args = parser.parse_args()
    
    # set seed
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.benchmark = False
    torch.use_deterministic_algorithms = True
    torch.backends.cudnn.deterministic = True

    args.save_name = make_model_name(**vars(args))

    # data_dir = get_datadir() / args.dataset / args.data_name / args.training_data_name
    training_data_dir = pathlib.Path(args.training_data_dir)
    route_data_path = None
    save_dir = training_data_dir / args.save_name
    (save_dir / "imgs").mkdir(exist_ok=True, parents=True)
    # save_path.mkdir(exist_ok=True, parents=True)
    # (save_path / "imgs").mkdir(exist_ok=True, parents=True)
    # args.save_path = str(save_path)

    # set logger
    logger = set_logger(__name__, save_dir / "log.log")
    logger.info('log is saved to {}'.format(save_dir / "log.log"))
    logger.info(f'used parameters {vars(args)}')

    args.consistent = args.consistent and args.train_all_layers
    # if args.consistent and not args.train_all_layers:
        # args.consistent = False
        # logger.info("!!!!!! consistent is set as False because train_all_layers is False")
    if args.network_type != "hiemrnet":
        args.train_all_layers = False

    logger.info(f"load training data from {training_data_dir / 'training_data.csv'}")
    logger.info(f"load time data from {training_data_dir / 'training_data_time.csv'}")
    dataset = construct_dataset(training_data_dir / "training_data.csv", route_data_path, args.n_split)

    device = torch.device(f"cuda:{args.cuda_number}" if torch.cuda.is_available() else "cpu")

    if args.batch_size == 0:
        args.batch_size = int(np.sqrt(len(dataset)))
        logger.info("batch size is set as " + str(args.batch_size))
        
    data_loader = torch.utils.data.DataLoader(dataset, num_workers=0, shuffle=True, pin_memory=True, batch_size=args.batch_size, collate_fn=dataset.make_padded_collate(args.remove_first_value, args.remove_duplicate))
    logger.info(f"len of the dataset: {len(dataset)}")

    if args.meta_n_iter == 0:
        args.epsilon = 0
        logger.info("pre-training is not done")
    else:
        if args.epsilon == 0:
            # decide the budget for the pre-training (this is for depth_clustering with depth = 2)
            args.epsilon = set_budget(len(dataset), int(np.sqrt(dataset.n_locations)) -2)
            logger.info(f"epsilon is set as: {args.epsilon} by our method")
        else:
            logger.info(f"epsilon is fixed as: {args.epsilon}")

    meta_network, location_to_class = construct_meta_network(args.clustering, args.network_type, dataset.n_locations, args.memory_dim, args.memory_hidden_dim, args.location_embedding_dim, args.multilayer, args.consistent, logger)
    meta_network.to(device)
    meta_network = pre_training_meta_network(meta_network, dataset, location_to_class, args.transition_type)

    generator, loss_model = construct_generator(dataset.n_locations, meta_network, args.network_type, args.location_embedding_dim, args.n_split, len(dataset.label_to_reference), args.hidden_dim, dataset.reference_to_label, logger)
    args.num_params = compute_num_params(generator, logger)
    generator.to(device)
    optimizer = optim.Adam(generator.parameters(), lr=args.learning_rate)

    if args.is_dp:
        logger.info("privating the model")
        privacy_engine = PrivacyEngine(accountant=args.accountant_mode)
        generator, optimizer, data_loader = privacy_engine.make_private(module=generator, optimizer=optimizer, data_loader=data_loader, noise_multiplier=args.noise_multiplier, max_grad_norm=args.clipping_bound)
        eval_generator = generator._module
    else:
        logger.info("not privating the model")
        eval_generator = generator

    early_stopping = EarlyStopping(patience=args.patience, verbose=True, path=save_dir / "checkpoint.pt", trace_func=logger.info)
    logger.info(f"early stopping patience: {args.patience}, save path: {save_dir / 'checkpoint.pt'}")


    logger.info(f"save param to {save_dir / 'params.json'}")
    with open(save_dir / "params.json", "w") as f:
        json.dump(vars(args), f)
    # if args.server:
        # send(save_dir / "params.json")

    # traning
    epsilon = 0
    for epoch in tqdm.tqdm(range(args.n_epochs)):

        # try:
        logger.info(f"save model to {save_dir / f'model_{epoch}.pt'}")
        torch.save(eval_generator.state_dict(), save_dir / f"model_{epoch}.pt")
        # if args.server:
            # send(save_path / f"model_{epoch}.pt")
        # except:
        # logger.info("failed to save model because it is Markov1?")

        # training
        if not args.is_dp:
            losses = train_epoch(data_loader, generator, optimizer)
        else:
            with BatchMemoryManager(data_loader=data_loader, max_physical_batch_size=min([args.physical_batch_size, args.batch_size]), optimizer=optimizer) as new_data_loader:
                losses = train_epoch(new_data_loader, generator, optimizer)
            epsilon = privacy_engine.get_epsilon(args.dp_delta)

        # early stopping
        early_stopping(np.sum(losses[:-1]), eval_generator)
        logger.info(f'epoch: {early_stopping.epoch} epsilon: {epsilon} | best loss: {early_stopping.best_score} | current loss: location {losses[:-2]}, time {losses[-2]}, norm {losses[-1]}')
        if early_stopping.early_stop:
            break
    
    # send(save_path / f"log.log")