import torch
import numpy as np
from torch.autograd import Variable
import torch.optim as optim
import sys

def reduce_sum(x, keepdim=True):
	for a in reversed(range(1, x.dim())):
		x = x.sum(a, keepdim=keepdim)

	return x


def L2_dist(x, y):
	return reduce_sum((x - y)**2)


class FGSM(object):
	def __init__(self, epsilon=0.25):
		self.epsilon = epsilon

	def attack(self, inputs, labels, model):
		"""
		Given a set of inputs and epsilon, return the perturbed inputs (as Variable objects).
		"""
		adv_inputs = inputs.data + self.epsilon * torch.sign(inputs.grad.data)
		adv_inputs = torch.clamp(adv_inputs, -1.0, 1.0)
		return Variable(adv_inputs, requires_grad=False)


class CarliniWagner(object):
	def __init__(self, confidence=0, learning_rate=1e-3, binary_search_steps=5, max_iterations=1000, 
		initial_const=0.01, num_labels=10, clip_min=-1, clip_max=1):
		"""
		Return a tensor that constructs adversarial examples for the given input.
		Only supports untargeted attacks.

		- confidence : Confidence of adversarial examples: higher produces examples 
			with larger l2 distortion, but more strongly classified as adversarial. 
			Set to 0 in the paper.
		- learning_rate : The learning rate for the attack algorithm. Smaller values produce 
			better results but are slower to converge.
		- binary_search_steps : The number of times we perform binary search to find the 
			optimal tradeoff-constant between norm of the perturbation and confidence 
			of the classification.
		- max_iterations : The maximum number of iterations. Setting this to a larger value 
			will produce lower distortion results. Using only a few iterations requires
			a larger learning rate, and will produce larger distortion results.
		- initial_const : The initial tradeoff-constant to use to tune the relative 
			importance of size of the perturbation and confidence of classification.
			If binary_search_steps is large, the initial constant is not important. 
			A smaller value of this constant gives lower distortion results.
			This is c in the formula in the paper.
		- clip_min : Minimum input component value.
		- clip_max : Maximum input component value.
		- num_labels : Number of classes in the model's output.
		"""
		self.confidence = confidence
		self.learning_rate = learning_rate
		self.initial_const = initial_const
		self.num_labels = num_labels
		self.shape = shape

		self.binary_search_steps = binary_search_steps
		self.repeat = binary_search_steps >= 10
		self.max_iterations = max_iterations
		
		# allows early aborts if gradient descent is unable to make progress 
		self.abort_early = True

		self.clip_min = clip_min
		self.clip_max = clip_max
		self.cuda = torch.cuda.is_available()

	def _compare(self, prediction, label):
		"""
		Return True if label is not the most likely class.
		If there is a prediction for each class, prediction[label] should be at least
		self.confidence from being the most likely class. 
		"""
		if not isinstance(prediction, (float, int, np.int64)):
			prediction = np.copy(prediction)	
			prediction[label] += self.confidence
			prediction = np.argmax(prediction)		

		return prediction != label 

	def _optimize(self, model, optimizer, modifier, inputs, labels, scale_const):
		"""
		Calculate loss and optimize for modifier here. Return the loss, adversarial inputs,
		and predicted classes. Since the attack is untargeted, aim to make label the least 
		likely class.

		modifier is the variable we're optimizing over (w in the paper).
		Don't think of it as weights in a NN; there is a unique w for each x in the batch.
		"""
		inputs_adv = (torch.tanh(modifier + inputs) + 1) * 0.5
		inputs_adv = inputs_adv * (self.clip_max - self.clip_min) + self.clip_min
		# outputs BEFORE SOFTMAX
		predicted = model(inputs_adv)	

		# before taking the L2 distance between the original and perturbed inputs,
		# transform the original inputs in the same way (arctan, then clip)
		unmodified = (torch.tanh(inputs) + 1) * 0.5
		unmodified = unmodified * (self.clip_max - self.clip_min) + self.clip_min
		dist = L2_dist(input_adv, unmodified).sum()
		loss2 = dist		

		# compute probability of label class and maximum other
		real = (labels * predicted).sum(1)
		other = ((1. - label) * predicted - label * 10000.).max(1)[0]

		# the greater the likelihood of label, the greater the loss
		loss1 = torch.clamp(real - other + self.confidence, min=0.)  # equiv to max(..., 0.)
		loss1 = torch.sum(scale_const * loss1)
		loss = loss1 + loss2 		

		optimizer.zero_grad()
		loss.backward()
		optimizer.step()

		# convert to numpy form before returning it 
		loss = loss.data.cpu().numpy()[0]
		dist = dist.data.cpu().numpy()
		predicted = predicted.data.cpu().numpy()
		# input_adv = input_adv.data.permute(0, 2, 3, 1).cpu().numpy()

		return loss, dist, predicted, input_adv

	def attack(self, inputs, labels, model):
		"""
                Given a set of inputs, labels, and the model, return the perturbed inputs (as Variable objects).
		inputs and labels should be Variable objects themselves.
                """
		batch_size = inputs.size(0)

		# reverse the transformation that will happen in _optimize for the inputs
		input_vars = (inputs - self.clip_min) / (self.clip_max - self.clip_min)
		input_vars = input_vars * 2 - 1
		input_vars = input_vars * (1 - 1e-6)
		input_vars = (torch.log((1 + input_vars) / (1 - input_vars))) * 0.5 # arctanh

		# set the lower and upper bounds accordingly
		lower_bound = np.zeros(batch_size)
		scale_const = np.ones(batch_size) * self.initial_const
		upper_bound = np.ones(batch_size) * 1e10
	
		# numpy placeholders for the overall best l2, most likely label, and adversarial image
		o_best_l2 = [1e10] * batch_size
		o_best_score = [-1] * batch_size
		o_best_attack = inputs
			
		# one-hot encoding of labels
		one_hot_labels = torch.zeros(labels.size() + (self.num_classes,))
		if self.cuda: one_hot_labels = one_hot_labels.cuda()
		one_hot_labels.scatter_(1, labels.unsqueeze(1), 1)
		label_vars = Variable(one_hot_labels, requires_grad=False)

		# setup the modifier variable; this is the variable we are optimizing over
		modifier_var = Variable(
			torch.zeros(input_vars.size()).cuda() if self.cuda else torch.zeros(input_vars.size()),
			requires_grad=False
		)

		optimizer = optim.Adam([modifier_var], lr=self.learning_rate)
		
		for outer_step in range(self.binary_search_steps):
			print 'search step: {0}'.format(outer_step)
			best_l2 = [1e10] * batch_size
			best_score = [-1] * batch_size

			# last iteration (if we run many steps) repeat the search once
			if self.repeat and search_step == self.binary_search_steps - 1:
				scale_const = upper_bound

			scale_const_var = Variable(
				torch.from_numpy(scale_const).cuda() if self.cuda else torch.from_numpy(scale_const),
				requires_grad=False
			)

			prev_loss = 1e-6	# for early abort

			for step in range(self.max_steps): 
				loss, dist, predicted, input_adv = self._optimize(model, optimizer, modifier_var, 
					input_vars, label_vars, scale_const_var)	

				if step % 100 == 0 or step == self.max_steps - 1:
					print "Step: {0:>4}, loss: {1:6.4f}, dist: {2:8.5f}, modifier mean: {3:.5e}".format(
						step, loss, dist.mean(), modifier_var.data.mean())

				# abort early if loss is too small
				if self.abort_early and step % (self.max_steps // 10) == 0:
					if loss > prev_loss * 0.9999:
						print 'Aborting early...'
						break

					prev_loss = loss
				
				# update best result for each image
				for i in range(batch_size):
					y_hat = np.argmax(predicted[i])

					# if smaller perturbation and still different predicted class ... 
					if dist[i] < best_l2[i] and self._compare(prediction[i], labels[i]):
						best_l2[i] = dist[i]
						best_score[i] = y_hat

					# update overall best results
					if dist[i] < o_best_l2[i] and self._compare(prediction[i], labels[i]):
						o_best_l2[i] = dist[i]
						o_best_score[i] = y_hat
						o_best_attack[i] = input_adv[i]					
		
				sys.stdout.flush()	

			# adjust constants
			batch_failure, batch_success = 0, 0
		
			for i in range(batch_size):
				if self._compare(best_score[i], labels[i]) and best_score[i] != -1:
					# successful, do binary search and divide const by two
					upper_bound[i] = min(upper_bound[i], scale_const[i])
					
					if upper_bound[i] < 1e9:
						scale_const[i] = (lower_bound[i] + upper_bound[i]) / 2
				else:
					# failure, multiply by 10 if no solution found
					# or do binary search with the known upper bound
					lower_bound[i] = max(lower_bound[i], scale_const[i])
					upper_bound[i] = (lower_bound[i] + upper_bound[i]) / 2 if (upper_bound[i] < 1e9) else (scale_const[i] * 10)
				
				if self._compare(o_best_score[i], label[i]) and o_best_score[i] != -1:
					batch_success += 1
				else:
					batch_failure += 1

			print 'failures: {0} successes: {1}'.format(batch_failure, batch_success)
			sys.stdout.flush()

		return o_best_attack
	