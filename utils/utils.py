import copy
import torch
from torch import nn
from torch.utils.data import DataLoader
from torchtext.data import Batch

def averge_models(models, device=None):
	final_model = copy.deepcopy(models[0])
	if device:
		models = [model.to(device) for model in models]
		final_model = final_model.to(device)

	averaged_parameters = aggregate_gradient_updates([list(model.parameters()) for model in models], mode='mean')
	
	for param, avg_param in zip(final_model.parameters(), averaged_parameters):
		param.data = avg_param.data
	return final_model


def compute_grad_update(old_model, new_model, device=None):
	# maybe later to implement on selected layers/parameters
	if device:
		old_model, new_model = old_model.to(device), new_model.to(device)
	return [(new_param.data - old_param.data) for old_param, new_param in zip(old_model.parameters(), new_model.parameters())]

def add_gradient_updates(grad_update_1, grad_update_2, weight = 1.0):
	assert len(grad_update_1) == len(
		grad_update_2), "Lengths of the two grad_updates not equal"
	
	for param_1, param_2 in zip(grad_update_1, grad_update_2):
		param_1.data += param_2.data * weight
	return grad_update_1

def aggregate_gradient_updates(grad_updates, R, device=None, mode='sum', credits=None, shard_sizes=None):
	if grad_updates:
		len_first = len(grad_updates[0])
		assert all(len(i) == len_first for i in grad_updates), "Different shapes of parameters. Cannot aggregate."
	else:
		return

	grad_updates_ = [copy.deepcopy(grad_update) for i, grad_update in enumerate(grad_updates) if i in R]

	if device:
		for i, grad_update in enumerate(grad_updates_):
			grad_updates_[i] = [param.to(device) for param in grad_update]

	if credits is not None:
		credits = [credit for i, credit in enumerate(credits) if i in R]
	if shard_sizes is not None:
		shard_sizes = [shard_size for i,shard_size in enumerate(shard_sizes) if i in R]

	aggregated_gradient_updates = []
	if mode=='mean':
		# default mean is FL-avg: weighted avg according to nk/n
		if shard_sizes is None:
			shard_sizes = torch.ones(len(grad_updates))

		for i, (grad_update, shard_size) in enumerate(zip(grad_updates_, shard_sizes)):
			grad_updates_[i] = [(shard_size * update) for update in grad_update]
		for i in range(len(grad_updates_[0])):
			aggregated_gradient_updates.append(torch.stack(
				[grad_update[i] for grad_update in grad_updates_]).mean(dim=0))

	elif mode =='sum':
		for i in range(len(grad_updates_[0])):
			aggregated_gradient_updates.append(torch.stack(
				[grad_update[i] for grad_update in grad_updates_]).sum(dim=0))

	elif mode == 'credit-sum':
		# first changes the grad_updates altogether
		for i, (grad_update, credit) in enumerate(zip(grad_updates_, credits)):
			grad_updates_[i] = [(credit * update) for update in grad_update]

		# then compute the credit weight sum
		for i in range(len(grad_updates_[0])):
			aggregated_gradient_updates.append(torch.stack(
				[grad_update[i] for grad_update in grad_updates_]).sum(dim=0))

	return aggregated_gradient_updates

def add_update_to_model(model, update, weight=1.0, device=None):
	if not update: return model
	if device:
		model = model.to(device)
		update = [param.to(device) for param in update]
			
	for param_model, param_update in zip(model.parameters(), update):
		param_model.data += weight * param_update.data
	return model

def compare_models(model1, model2):
	for p1, p2 in zip(model1.parameters(), model2.parameters()):
		if p1.data.ne(p2.data).sum() > 0:
			return False # two models have different weights
	return True


def flatten(grad_update):
	return torch.cat([update.data.view(-1) for update in grad_update])

def unflatten(flattened, normal_shape):
	grad_update = []
	for param in normal_shape:
		n_params = len(param.view(-1))
		grad_update.append(  torch.as_tensor(flattened[:n_params]).reshape(param.size())  )
		flattened = flattened[n_params:]

	return grad_update

def evaluate(model, eval_loader, device, loss_fn=None, verbose=True):
	model.eval()
	model = model.to(device)
	correct = 0
	total = 0

	with torch.no_grad():
		for i, batch in enumerate(eval_loader):

			if isinstance(batch, Batch):
				batch_data, batch_target = batch.text, batch.label
				# batch_data.data.t_(), batch_target.data.sub_(1)  # batch first, index align
				batch_data = batch_data.permute(1, 0)
			else:
				batch_data, batch_target = batch[0], batch[1]

			batch_data, batch_target = batch_data.to(device), batch_target.to(device)

			outputs = model(batch_data)

			if loss_fn:
				loss = loss_fn(outputs, batch_target)
			else:
				loss = None
			correct += (torch.max(outputs, 1)[1].view(batch_target.size()).data == batch_target.data).sum()
			total += len(batch_target)
		accuracy =  correct.float() / total
	
	if verbose:
		print("Loss: {:.6f}. Accuracy: {:.4%}.".format(loss, accuracy))
	return loss, accuracy

'''
def one_on_one_evaluate(participants, federated_model, grad_updates, unfiltererd_grad_updates, eval_loader, device):
	val_accs = []
	for i, participant in enumerate(participants):
		if participant.theta == 1:
			model_to_eval = copy.deepcopy(participant.model)
			add_update_to_model(model_to_eval, unfiltererd_grad_updates[i], device=device)
		else:
			model_to_eval = copy.deepcopy(federated_model)
			add_update_to_model(model_to_eval, grad_updates[i], device=device)

		_, val_acc = evaluate(model_to_eval, eval_loader, device, verbose=False)
		del model_to_eval
		val_accs.append(val_acc)
	return val_accs


def leave_one_out_evaluate(federated_model, grad_updates, eval_loader, device):
	loo_model = copy.deepcopy(federated_model)
	loo_losses, loo_val_accs = [], []
	for grad_update in grad_updates:
		loo_model = add_update_to_model(loo_model, grad_update, weight = -1.0, device=device)
		loss, val_acc = evaluate(loo_model, eval_loader, device, verbose=False)
		loo_losses.append(loss)
		loo_val_accs.append(val_acc)
		loo_model = add_update_to_model(loo_model, grad_update, weight = 1.0, device=device)

	# scalar - 1D torch tensor subtraction -> 1D torch tensor
	# marginal_contributions = curr_val_acc - torch.tensor(loo_val_accs) 

	return  loo_val_accs
'''
import numpy as np
np.random.seed(1111)


def random_split(sample_indices, m_bins, equal=True):
	sample_indices = np.asarray(sample_indices)
	if equal:
		indices_list = np.array_split(sample_indices, m_bins)
	else:
		split_points = np.random.choice(
			n_samples - 2, m_bins - 1, replace=False) + 1
		split_points.sort()
		indices_list = np.split(sample_indices, split_points)

	return indices_list


import random
from itertools import permutations

def compute_shapley(grad_updates, federated_model, test_loader, device, Max_num_sequences=50):
	num_participants = len(grad_updates)
	all_sequences = list(permutations(range(num_participants)))
	if len(all_sequences) > Max_num_sequences:
		random.shuffle(all_sequences)
		all_sequences = all_sequences[:Max_num_sequences]

	test_loss_prev, test_acc_prev = evaluate(federated_model, test_loader, device, verbose=False)
	prev_contribution = test_acc_prev.data
	
	marginal_contributions = torch.zeros((num_participants))
	for sequence in all_sequences:
		running_model = copy.deepcopy(federated_model)
		curr_contributions = []
		for participant_id in sequence:
			running_model = add_update_to_model(running_model, grad_updates[participant_id])
			test_loss, test_acc = evaluate(running_model, test_loader, device, verbose=False)
			contribution = test_acc.data

			if not curr_contributions:
				marginal_contributions[participant_id] +=  contribution - prev_contribution
			else:
				marginal_contributions[participant_id] +=  contribution - curr_contributions[-1]

			curr_contributions.append(contribution)

	return marginal_contributions / len(all_sequences)