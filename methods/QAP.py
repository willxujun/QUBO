import numpy as np
import itertools
import math
import time

from problems.bunching import BunchingQAP
from problems.placement import PlacementQAP
from problems.grouping import GroupingProblem
from problems.QMKP import QMKP
from ports.dwave import Dwave
from ports.da.da_solver import DASolver
from ports.classical_simanneal import ClassicalNeal
from .exterior_penalty import ExteriorPenaltyMethod
import utils.index as idx

class OurHeuristic:
    def __init__(self,n,m,k,F,D, fine_weight0, fine_alpha0, const_weight_inc=False, use_dwave_da_sw="dwave"):
        self.n = n
        self.m = m
        self.k = k
        self.F = F
        self.D = D
        self.use_dwave_da_sw = use_dwave_da_sw

        self.fine_weight0 = fine_weight0
        self.fine_alpha0 = fine_alpha0
        self.const_weight_inc = const_weight_inc
        self.timing = []
    
    def get_timing(self):
        return self.timing
    

    def get_feasible_solution(self, members, locations, solution2):
        ret = np.zeros((self.n, self.m))
        k = len(members)
        for i in range(k):
            for j in range(k):
                if solution2[i][j]:
                    members_list = members[i]
                    locations_list = locations[j]
                    for m, l in zip(members_list, locations_list):
                        ret[m][l] = 1
        return ret

    def specialise_bunch(
        self, 
        initial_solution,
        bunch_i_idx_map,
        locations_i_idx_map,
    ):
        bunch_size = len(bunch_i_idx_map.keys())
        linear = np.zeros(bunch_size*bunch_size)

        # collect a list of item index pairs in the bunch, arranged in
        # order of linear's corresponding local indices,
        variable_list = np.empty(shape=bunch_size*bunch_size,dtype=object)
        # print(bunch_i_idx_map.values())
        # print(locations_i_idx_map.values())
        # input()
        for item_global_idx, item_local_idx in bunch_i_idx_map.items():
            for loc_global_idx, loc_local_idx in locations_i_idx_map.items():
                # note that local indices run from 0 to bunch_size-1
                local_variable_index = idx.index_1_q_to_l_1(item_local_idx+1,loc_local_idx+1,bunch_size) - 1
                variable_list[local_variable_index]= (item_global_idx, loc_global_idx)
        
        # extract the columns of F and D in variable_list order
        columns_F, columns_D = zip(*variable_list)
        F1 = self.F[:, columns_F]
        D1 = self.D[:, columns_D]

        linear = np.zeros(shape=bunch_size*bunch_size,dtype=np.float32)
        for n0 in range(self.n):
            for m0 in range(self.m):
                if initial_solution[n0][m0]:
                    linear += F1[n0, :] * D1[m0, :]

        return linear
            
    def run(self):
        start = time.time()
        print("setting up bunching with simanneal")
        bunch = QMKP(
            -self.F,
            self.k
        )
        bunch_auto_schedule = bunch.auto(minutes=1)
        bunch.set_schedule(bunch_auto_schedule)
        bunch.copy_strategy = "deepcopy"
        print("starting to solve bunching with simanneal")
        state1, energy1 = bunch.anneal()
        bunch_end = time.time()
        print(state1)
        print("done bunching with simanneal. energy: %d" % energy1)
        solution1 = QMKP.solution_matrix(state1, self.n, self.k)
        print(solution1)
        self.timing.append(bunch_end - start)
        

        #######grouping########
        print("setting up grouping with simanneal")
        group_start = time.time()
        group = QMKP(
            self.D,
            self.k
        )
        group_auto_schedule = group.auto(minutes=1)
        group.set_schedule(group_auto_schedule)
        group.copy_strategy = "deepcopy"
        print("starting to solve grouping with simanneal")
        state1_5, energy1_5 = group.anneal()
        group_end = time.time()
        print("Done grouping with simanneal. Energy: %d" % energy1_5)
        solution1_5 = QMKP.solution_matrix(state1_5, self.n, self.k)
        print(solution1_5)
        self.timing.append(group_end - group_start)

        #######bunch permutation/ aggregate QAP########
        g = np.zeros(shape=self.n)
        members = []
        for i in range(self.k):
            members.append([])
        for i in range(self.n):
            for j in range(self.k):
                if solution1[i][j]:
                    g[i]=j
                    members[j].append(i)
        
        bigF = np.zeros((self.k,self.k))
        for i1 in range(self.k):
            for i2 in range(i1 + 1,self.k):
                interaction = 0
                for item1,item2 in itertools.product(members[i1],members[i2]):
                    interaction += self.F[item1][item2]
                bigF[i1][i2] = interaction

        locations = []
        for i in range(self.k):
            locations.append([])
        for i in range(self.m):
            for j in range(self.k):
                if solution1_5[i][j]:
                    locations[j].append(i)

        bigD = np.zeros((self.k,self.k))
        for j1 in range(self.k):
            for j2 in range(j1+1,self.k):
                distance = 0
                for loc1, loc2 in itertools.product(locations[j1], locations[j2]):
                    distance += self.D[loc1][loc2]
                bigD[j1][j2] = distance

        print("=====================computing aggregate placement=========================")
        aggregate_placement_problem = PlacementQAP(
            self.k,
            self.k,
            bigF,
            bigD,
            initial_weight_estimate=True,
            const_weight_inc=True
        )
        dwave_solver = ClassicalNeal()
        aggregate_method = ExteriorPenaltyMethod(
            aggregate_placement_problem,
            dwave_solver,
            100000000
        )
        solution2 = aggregate_placement_problem.solution_matrix(
            (aggregate_method.run())[0],
            self.k,
            self.k
        )
        self.timing.append(aggregate_placement_problem.timing)
        self.timing.append(aggregate_method.get_timing())

        bunch_to_group = {}
        for i in range(self.k):
            for j in range(self.k):
                if solution2[i][j]:
                    bunch_to_group[i] = j

        #######placement within bunches###########
        print("=======================computing fine placement============================")
        ret = np.zeros((self.n, self.m))
        s = (int)(math.floor(self.m / self.k))
        bunch_size = (int)(math.ceil(self.n / self.k))

        solution3 = {}
        # np.set_printoptions(threshold=np.inf)
        # print(members)
        # print(locations)

        initial_solution = self.get_feasible_solution(members, locations, solution2)
        #print(initial_solution)

        timing_construction = 0
        timing_anneal = 0
        for i in range(self.k):
            bunch_i = np.sort(members[i])
            locations_i = np.sort(locations[bunch_to_group[i]])

            # idx_map is from global to local
            # inverse is from local to global
            bunch_i_idx_map = {}
            bunch_i_idx_map_inv = {}
            locations_i_idx_map = {}
            locations_i_idx_map_inv = {}
            for a in range(bunch_size):
                bunch_i_idx_map[bunch_i[a]] = a
                bunch_i_idx_map_inv[a] = bunch_i[a]
            for b in range(s):
                locations_i_idx_map[locations_i[b]] = b
                locations_i_idx_map_inv[b] = locations_i[b]
            FPrime = np.zeros((bunch_size,bunch_size))
            DPrime = np.zeros((s,s))

            for i1,i2 in itertools.product(bunch_i,bunch_i):
                FPrime[bunch_i_idx_map[i1]][bunch_i_idx_map[i2]] = self.F[i1][i2]

            for j1,j2 in itertools.product(locations_i,locations_i):
                DPrime[locations_i_idx_map[j1]][locations_i_idx_map[j2]] = self.D[j1][j2]

            print("+++++specialising %d bunch +++++++" % i)
            linear=self.specialise_bunch(
                initial_solution,
                bunch_i_idx_map,
                locations_i_idx_map
            )
            print("done")
            print(linear)
            
            print("+++++constructing %d bunch QAP +++++++" % i)
            fine_placement_problem = PlacementQAP(
                bunch_size,
                s,
                FPrime,
                DPrime,
                initial_weight_estimate=True,
                const_weight_inc=True,
                linear=linear
            )
            print("done")
            if self.use_dwave_da_sw == 'dwave':
                solver_i = Dwave()
            elif self.use_dwave_da_sw == 'sw':
                solver_i = ClassicalNeal()
            elif self.use_dwave_da_sw == 'da':
                solver_i = DASolver()
            fine_placement_method = ExteriorPenaltyMethod(
                fine_placement_problem,
                solver_i,
                100000000
            )
            print("done")
            print("+++++running %d bunch QAP +++++++" % i)
            timing_construction += fine_placement_problem.timing
            solution3[i] = PlacementQAP.solution_matrix(
                (fine_placement_method.run())[0],
                bunch_size,
                s
            )
            print("done")
            timing_anneal += fine_placement_method.get_timing()
            
            np.set_printoptions(threshold=np.inf)
            #print(bunch_i, locations_i)

            for local_item in range(bunch_size):
                for local_loc in range(s):
                    global_item = bunch_i_idx_map_inv[local_item]
                    global_loc = locations_i_idx_map_inv[local_loc]
                    # if((solution3[i])[local_item][local_loc]):
                    #     print(global_item, global_loc)
                    initial_solution[global_item][global_loc] = (solution3[i])[local_item][local_loc]
                    ret[global_item][global_loc] = (solution3[i])[local_item][local_loc]
        check = PlacementQAP.check_mtx(ret)
        if not all(check):
            raise ValueError("unfeasible solution error")
        
        self.timing.append(timing_construction)
        self.timing.append(timing_anneal)
        end = time.time()
        self.timing.append(end-start)
        
        return ret
        