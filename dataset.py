from torch.utils.data import Dataset
import torch
import numpy as np

def make_format_to_label(traj_list):
    format_to_label = {}
    for trajectory in traj_list:
        traj_type = traj_to_format(trajectory)
        if traj_type not in format_to_label:
            format_to_label[traj_type] = len(format_to_label)
    return format_to_label

def make_label_to_format(format_to_label):
    label_to_format = {}
    for format in format_to_label:
        label = format_to_label[format]
        label_to_format[label] = format
    return label_to_format
    

def make_label_info(real_traj):
    # make dictionary that maps a format to a label
    format_to_label = make_format_to_label(real_traj)
    # label_to_format
    label_to_format = make_label_to_format(format_to_label)

    return format_to_label, label_to_format

def traj_to_format(traj):
    # list a set of states in the trajectory
    # i.e., remove the duplicated states
    states = []
    for state in traj:
        if state not in states:
            states.append(state)
    # convert the list of states to a string
    # i.e., convert the list of states to a format
    format = ''
    for state in traj:
        # convert int to alphabet
        format += chr(states.index(state) + 97)
        # format += str(states.index(state))

    return format

class TrajectoryDataset(Dataset):

    @staticmethod
    def start_idx(n_locations):
        return n_locations
    
    @staticmethod
    def ignore_idx(n_locations):
        return n_locations+1
    
    @staticmethod
    def end_idx(n_locations):
        return n_locations+2
    
    @staticmethod
    def vocab_size(n_locations):
        return n_locations+3
    
    @staticmethod
    def time_end_idx(n_split):
        return n_split

    def _time_end_idx(self):
        return TrajectoryDataset.time_end_idx(self.n_time_split)
    
    @staticmethod
    def time_to_label(time, n_time_split, max_time):
        if time == 0:
            return 0
        elif time == max_time:
            return n_time_split
        assert time <= max_time, f"time {time} is larger than max_time {max_time}"
        return int(time//(max_time/n_time_split))+1
    
    def _time_to_label(self, time):
        return TrajectoryDataset.time_to_label(time, self.n_time_split, self.max_time)

    @staticmethod
    def label_to_time(label, n_time_split, max_time):
        return max([int((label-1)*max_time/n_time_split),0])
    
    def _label_to_time(self, label):
        return TrajectoryDataset.label_to_time(label, self.n_time_split, self.max_time)
    
    def label_to_length(self, label):
        format = self.label_to_format[label]
        return len(format)
    
    def make_reference(self, label):
        format = self.label_to_format[label]
        reference = {}
        for i in range(len(format)):
            if format[i] not in reference:
                reference[format[i]] = i
                
        return tuple([reference[format[i]] for i in range(len(format))])
    
    def _make_label_to_reference(self):
        
        return {label: self.make_reference(label) for label in self.label_to_format.keys()}
    
    #Init dataset
    def __init__(self, data, time_data, n_locations, n_time_split, real_start=True, dataset_name="dataset", route_data=None):
        assert len(data) == len(time_data)
        
        self.data = data
        self.seq_len = max([len(trajectory) for trajectory in data])
        self.min_len = min([len(trajectory) for trajectory in data])
        self.time_data = time_data
        self.n_locations = n_locations
        self.n_bins = int(np.sqrt(n_locations)-2)
        self.dataset_name = dataset_name
        self.format_to_label, self.label_to_format = make_label_info(data)
        self.labels = self._compute_dataset_labels()
        self.label_to_reference = self._make_label_to_reference()
        self.reference_to_label_ = {reference: label for label, reference in self.label_to_reference.items()}
        self.references = [self.label_to_reference[label] for label in self.labels]
        if real_start:
            self.references = [tuple([traj[0]] + list(reference[1:])) for reference, traj in zip(self.references, self.data)]
        else:
            self.references = [tuple([-1] + list(reference[1:])) for reference in self.references]
        self.n_time_split = n_time_split
        self.max_time = max([max(time_traj) for time_traj in time_data])

        self.time_label_trajs = []
        for time_traj in self.time_data:
            self.time_label_trajs.append(tuple([self._time_to_label(t) for t in time_traj]))

        self.time_ranges = [(self._label_to_time(i), self._label_to_time(i+1)) for i in range(n_time_split)]
        self.computed_auxiliary_information = False

        self.n_bins_for_distance = 30

        if route_data is not None:
            assert len(route_data) == len(data)
            print("route data is given")
            self.route_data = route_data

        else:
            print("route_data is not given")
            self.route_data = data
            print("use trajectory data as route data")

    def reference_to_label(self, reference):
        reference = tuple([0] + list(reference[1:]))
        return self.reference_to_label_[reference]

    def __str__(self):
        return self.dataset_name
        
    # fetch data
    def __getitem__(self, index):
        trajectory = self.data[index]
        time_trajectory = list(self.time_label_trajs[index])
        
        return {'trajectory': trajectory, 'time_trajectory': time_trajectory}

    def __len__(self):
        return len(self.data)
    
    def _compute_dataset_labels(self):
        labels = [self.format_to_label[traj_to_format(trajectory)] for trajectory in self.data]
        return labels
    
    def convert_time_label_trajs_to_time_trajs(self, time_label_trajs):
        time_trajs = []
        for time_label_traj in time_label_trajs:
            time_trajs.append(self.convert_time_label_traj_to_time_traj(time_label_traj))
        return time_trajs
    
    def convert_time_label_traj_to_time_traj(self, time_label_traj):
        time_traj = []
        for time_label in time_label_traj:
            time_traj.append(self._label_to_time(time_label))
        return time_traj

    def make_padded_collate(self, remove_first_value=False, remove_duplicate=False):
        start_idx = TrajectoryDataset.start_idx(self.n_locations)
        ignore_idx = TrajectoryDataset.ignore_idx(self.n_locations)
        time_end_idx = TrajectoryDataset.time_end_idx(self.n_time_split)

        def padded_collate(batch):
            max_len = max([len(x["trajectory"]) for x in batch])
            inputs = []
            targets = []
            times = []
            target_times = []
            references = []

            for record in batch:
                trajectory = record["trajectory"]
                time_trajecotry = record["time_trajectory"]

                format = traj_to_format(trajectory)
                label = self.format_to_label[format]
                reference = self.label_to_reference[label]

                input = [start_idx] + trajectory + [ignore_idx] * (max_len - len(trajectory))
                target = input[1:] + [ignore_idx]

                # convert the duplicated state of target to the ignore_idx
                # if the label is "010", then the second 0 is converted to the ignore_idx
                if remove_duplicate:
                    checked_target = ["a"]
                    for i in range(1,len(format)):
                        if format[i] not in checked_target:
                            checked_target.append(format[i])
                            continue
                        target[i] = ignore_idx

                if remove_first_value:
                    target[0] = ignore_idx

                time_input = time_trajecotry + [time_end_idx] * (max_len - len(time_trajecotry)+1)
                time_target = time_input[1:] + [time_end_idx]

                inputs.append(input)
                targets.append(target)
                times.append(time_input)
                target_times.append(time_target)
                references.append(reference)
            
            return {"input":torch.Tensor(inputs).long(), "target":torch.Tensor(targets).long(), "time":torch.Tensor(times).long(), "time_target":torch.Tensor(target_times).long(), "reference":references}

        return padded_collate
        