import os
import sys
import numpy as np
import pandas as pd
import sympy
from sympy import sympify, lambdify
import re
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
import warnings
from multiprocessing import cpu_count
from sklearn.linear_model import LinearRegression
from sklearn.base import BaseEstimator, RegressorMixin, MultiOutputMixin
from sklearn.utils.validation import _check_feature_names_in, check_is_fitted

from .julia_helpers import (
    init_julia,
    _get_julia_project,
    is_julia_version_greater_eq,
    _escape_filename,
    _add_sr_to_julia_project,
    import_error_string,
)
from .deprecated import make_deprecated_kwargs_for_pysr_regressor


Main = None

already_ran = False

sympy_mappings = {
    "div": lambda x, y: x / y,
    "mult": lambda x, y: x * y,
    "sqrt_abs": lambda x: sympy.sqrt(abs(x)),
    "square": lambda x: x**2,
    "cube": lambda x: x**3,
    "plus": lambda x, y: x + y,
    "sub": lambda x, y: x - y,
    "neg": lambda x: -x,
    "pow": lambda x, y: abs(x) ** y,
    "cos": sympy.cos,
    "sin": sympy.sin,
    "tan": sympy.tan,
    "cosh": sympy.cosh,
    "sinh": sympy.sinh,
    "tanh": sympy.tanh,
    "exp": sympy.exp,
    "acos": sympy.acos,
    "asin": sympy.asin,
    "atan": sympy.atan,
    "acosh": lambda x: sympy.acosh(abs(x) + 1),
    "acosh_abs": lambda x: sympy.acosh(abs(x) + 1),
    "asinh": sympy.asinh,
    "atanh": lambda x: sympy.atanh(sympy.Mod(x + 1, 2) - 1),
    "atanh_clip": lambda x: sympy.atanh(sympy.Mod(x + 1, 2) - 1),
    "abs": abs,
    "mod": sympy.Mod,
    "erf": sympy.erf,
    "erfc": sympy.erfc,
    "log_abs": lambda x: sympy.log(abs(x)),
    "log10_abs": lambda x: sympy.log(abs(x), 10),
    "log2_abs": lambda x: sympy.log(abs(x), 2),
    "log1p_abs": lambda x: sympy.log(abs(x) + 1),
    "floor": sympy.floor,
    "ceil": sympy.ceiling,
    "sign": sympy.sign,
    "gamma": sympy.gamma,
}


def pysr(X, y, weights=None, **kwargs):  # pragma: no cover
    warnings.warn(
        "Calling `pysr` is deprecated. Please use `model = PySRRegressor(**params); model.fit(X, y)` going forward.",
        FutureWarning,
    )
    model = PySRRegressor(**kwargs)
    model.fit(X, y, weights=weights)
    return model.equations


def _handle_constraints(binary_operators, unary_operators, constraints):
    for op in unary_operators:
        if op not in constraints:
            constraints[op] = -1
    for op in binary_operators:
        if op not in constraints:
            constraints[op] = (-1, -1)
        if op in ["plus", "sub", "+", "-"]:
            if constraints[op][0] != constraints[op][1]:
                raise NotImplementedError(
                    "You need equal constraints on both sides for - and +, due to simplification strategies."
                )
        elif op in ["mult", "*"]:
            # Make sure the complex expression is in the left side.
            if constraints[op][0] == -1:
                continue
            if constraints[op][1] == -1 or constraints[op][0] < constraints[op][1]:
                constraints[op][0], constraints[op][1] = (
                    constraints[op][1],
                    constraints[op][0],
                )


def _create_inline_operators(binary_operators, unary_operators):
    global Main
    for op_list in [binary_operators, unary_operators]:
        for i, op in enumerate(op_list):
            is_user_defined_operator = "(" in op

            if is_user_defined_operator:
                Main.eval(op)
                # Cut off from the first non-alphanumeric char:
                first_non_char = [j for j, char in enumerate(op) if char == "("][0]
                function_name = op[:first_non_char]
                # Assert that function_name only contains
                # alphabetical characters, numbers,
                # and underscores:
                if not re.match(r"^[a-zA-Z0-9_]+$", function_name):
                    raise ValueError(
                        f"Invalid function name {function_name}. "
                        "Only alphanumeric characters, numbers, and underscores are allowed."
                    )
                op_list[i] = function_name


def _check_assertions(
    X,
    binary_operators,
    unary_operators,
    use_custom_variable_names,
    variable_names,
    weights,
    y,
):
    # Check for potential errors before they happen
    assert len(unary_operators) + len(binary_operators) > 0
    assert len(X.shape) == 2
    assert len(y.shape) in [1, 2]
    assert X.shape[0] == y.shape[0]
    if weights is not None:
        assert weights.shape == y.shape
        assert X.shape[0] == weights.shape[0]
    if use_custom_variable_names:
        assert len(variable_names) == X.shape[1]


def best(*args, **kwargs):  # pragma: no cover
    raise NotImplementedError(
        "`best` has been deprecated. Please use the `PySRRegressor` interface. After fitting, you can return `.sympy()` to get the sympy representation of the best equation."
    )


def best_row(*args, **kwargs):  # pragma: no cover
    raise NotImplementedError(
        "`best_row` has been deprecated. Please use the `PySRRegressor` interface. After fitting, you can run `print(model)` to view the best equation, or `model.get_best()` to return the best equation's row in `model.equations`."
    )


def best_tex(*args, **kwargs):  # pragma: no cover
    raise NotImplementedError(
        "`best_tex` has been deprecated. Please use the `PySRRegressor` interface. After fitting, you can return `.latex()` to get the sympy representation of the best equation."
    )


def best_callable(*args, **kwargs):  # pragma: no cover
    raise NotImplementedError(
        "`best_callable` has been deprecated. Please use the `PySRRegressor` interface. After fitting, you can use `.predict(X)` to use the best callable."
    )


class CallableEquation:
    """Simple wrapper for numpy lambda functions built with sympy"""

    def __init__(self, sympy_symbols, eqn, selection=None, variable_names=None):
        self._sympy = eqn
        self._sympy_symbols = sympy_symbols
        self._selection = selection
        self._variable_names = variable_names
        self._lambda = lambdify(sympy_symbols, eqn)

    def __repr__(self):
        return f"PySRFunction(X=>{self._sympy})"

    def __call__(self, X):
        expected_shape = (X.shape[0],)
        if isinstance(X, pd.DataFrame):
            # Lambda function takes as argument:
            return self._lambda(
                **{k: X[k].values for k in self._variable_names}
            ) * np.ones(expected_shape)
        if self._selection is not None:
            X = X[:, self._selection]
        return self._lambda(*X.T) * np.ones(expected_shape)


class PySRRegressor(BaseEstimator, RegressorMixin, MultiOutputMixin):
    """
    Symbolic regression - scikit-learn interface for SymbolicRegression.jl.

    Parameters
    ----------
    model_selection : str, default="best"
        Model selection criterion. Can be 'accuracy' or 'best'.
        `"accuracy"` selects the candidate model with the lowest loss
        (highest accuracy). `"best"` selects the candidate model with
        the lowest sum of normalized loss and complexity.

    binary_operators : list[str], default=["+", "-", "*", "/"]
        List of strings giving the binary operators in Julia's Base.

    unary_operators : list[str], default=[]
        Same as :param`binary_operators` but for operators taking a
        single scalar.

    niterations : int, default=40
        Number of iterations of the algorithm to run. The best
        equations are printed and migrate between populations at the
        end of each iteration.

    populations : int, default=15
        Number of populations running.

    population_size : int, default=33
        Number of individuals in each population.

    max_evals : int, default=None
        Limits the total number of evaluations of expressions to
        this number.

    maxsize : int, default=20
        Max size of an equation.

    maxdepth : int, default=None
        Max depth of an equation. You can use both :param`maxsize` and
        :param`maxdepth`. :param`maxdepth` is by default set to equal
        :param`maxsize`, which means that it is redundant.

    warmup_maxsize_by : float, default=0.0
        Whether to slowly increase max size from a small number up to
        the maxsize (if greater than 0).  If greater than 0, says the
        fraction of training time at which the current maxsize will
        reach the user-passed maxsize.

    timeout_in_seconds : float, default=None
        Make the search return early once this many seconds have passed.

    constraints : dict[str, int | tuple[int,int]], default={}
        Dictionary of int (unary) or 2-tuples (binary), this enforces
        maxsize constraints on the individual arguments of operators.
        E.g., `'pow': (-1, 1)` says that power laws can have any
        complexity left argument, but only 1 complexity exponent. Use
        this to force more interpretable solutions.

    nested_constraints : dict[str, dict], default=None
        Specifies how many times a combination of operators can be
        nested. For example, `{"sin": {"cos": 0}}, "cos": {"cos": 2}}`
        specifies that `cos` may never appear within a `sin`, but `sin`
        can be nested with itself an unlimited number of times. The
        second term specifies that `cos` can be nested up to 2 times
        within a `cos`, so that `cos(cos(cos(x)))` is allowed
        (as well as any combination of `+` or `-` within it), but
        `cos(cos(cos(cos(x))))` is not allowed. When an operator is not
        specified, it is assumed that it can be nested an unlimited
        number of times. This requires that there is no operator which
        is used both in the unary operators and the binary operators
        (e.g., `-` could be both subtract, and negation). For binary
        operators, you only need to provide a single number: both
        arguments are treated the same way, and the max of each
        argument is constrained.

    loss : str, default="L2DistLoss()"
        String of Julia code specifying the loss function. Can either
        be a loss from LossFunctions.jl, or your own loss written as a
        function. Examples of custom written losses include:
        `myloss(x, y) = abs(x-y)` for non-weighted, or
        `myloss(x, y, w) = w*abs(x-y)` for weighted.

        Among the included losses, these are as follows.
        Regression: `LPDistLoss{P}()`, `L1DistLoss()`,
        `L2DistLoss()` (mean square), `LogitDistLoss()`,
        `HuberLoss(d)`, `L1EpsilonInsLoss(ϵ)`, `L2EpsilonInsLoss(ϵ)`,
        `PeriodicLoss(c)`, `QuantileLoss(τ)`.
        Classification: `ZeroOneLoss()`, `PerceptronLoss()`,
        `L1HingeLoss()`, `SmoothedL1HingeLoss(γ)`,
        `ModifiedHuberLoss()`, `L2MarginLoss()`, `ExpLoss()`,
        `SigmoidLoss()`, `DWDMarginLoss(q)`.

    complexity_of_operators : dict[str, float], default=None
        If you would like to use a complexity other than 1 for an
        operator, specify the complexity here. For example,
        `{"sin": 2, "+": 1}` would give a complexity of 2 for each use
        of the `sin` operator, and a complexity of 1 for each use of
        the `+` operator (which is the default). You may specify real
        numbers for a complexity, and the total complexity of a tree
        will be rounded to the nearest integer after computing.

    complexity_of_constants : float, default=1
        Complexity of constants.

    complexity_of_variables : float, default=1
        Complexity of variables.

    parsimony : float, default=0.0032
        Multiplicative factor for how much to punish complexity.

    use_frequency : bool, default=True
        Whether to measure the frequency of complexities, and use that
        instead of parsimony to explore equation space. Will naturally
        find equations of all complexities.

    use_frequency_in_tournament : bool, default=True
        Whether to use the frequency mentioned above in the tournament,
        rather than just the simulated annealing.

    alpha : float, default=0.1
        Initial temperature for simulated annealing
        (requires :param`annealing` to be `True`).

    annealing : bool, default=True
        Whether to use annealing. You should (and it is default).

    early_stop_condition : float, default=None
        Stop the search early if this loss is reached.

    ncyclesperiteration : int, default=550
        Number of total mutations to run, per 10 samples of the
        population, per iteration.

    fraction_replaced : float, default=0.000364
        How much of population to replace with migrating equations from
        other populations.

    fraction_replaced_hof : float, default=0.035
        How much of population to replace with migrating equations from
        hall of fame.

    weight_add_node : float, default=0.79
        Relative likelihood for mutation to add a node.

    weight_insert_node : float, default=5.1
        Relative likelihood for mutation to insert a node.

    weight_delete_node : float, default=1.7
        Relative likelihood for mutation to delete a node.

    weight_do_nothing : float, default=0.21
        Relative likelihood for mutation to leave the individual.

    weight_mutate_constant : float, default=0.048
        Relative likelihood for mutation to change the constant slightly 
        in a random direction.

    weight_mutate_operator : float, default=0.47
        Relative likelihood for mutation to swap an operator.

    weight_randomize : float, default=0.00023
        Relative likelihood for mutation to completely delete and then 
        randomly generate the equation

    weight_simplify : float, default=0.0020
        Relative likelihood for mutation to simplify constant parts by evaluation

    crossover_probability : float, default=0.066
        Absolute probability of crossover-type genetic operation, instead of a mutation.

    skip_mutation_failures : bool, default=True
        Whether to skip mutation and crossover failures, rather than
        simply re-sampling the current member.

    migration : bool, default=True
        Whether to migrate.

    hof_migration : bool, default=True
        Whether to have the hall of fame migrate.

    topn : int, default=12
        How many top individuals migrate from each population.

    should_optimize_constants : bool, default=True
        Whether to numerically optimize constants (Nelder-Mead/Newton)
        at the end of each iteration.

    optimizer_algorithm : str, default="BFGS"
        Optimization scheme to use for optimizing constants. Can currently
        be `NelderMead` or `BFGS`.

    optimizer_nrestarts : int, default=2
        Number of time to restart the constants optimization process with
        different initial conditions.

    optimize_probability : float, default=0.14
        Probability of optimizing the constants during a single iteration of
        the evolutionary algorithm.

    optimizer_iterations : int, default=8
        Number of iterations that the constants optimizer can take.

    perturbation_factor : float, default=0.076
        Constants are perturbed by a max factor of
        (perturbation_factor*T + 1). Either multiplied by this or
        divided by this.

    tournament_selection_n : int, default=10
        Number of expressions to consider in each tournament.

    tournament_selection_p : float, default=0.86
        Probability of selecting the best expression in each
        tournament. The probability will decay as p*(1-p)^n for other
        expressions, sorted by loss.

    procs : int, default=multiprocessing.cpu_count()
        Number of processes (=number of populations running).

    multithreading : bool, default=True
        Use multithreading instead of distributed backend.
        Using procs=0 will turn off both.

    cluster_manager : str, default=None
        For distributed computing, this sets the job queue system. Set
        to one of "slurm", "pbs", "lsf", "sge", "qrsh", "scyld", or
        "htc". If set to one of these, PySR will run in distributed
        mode, and use `procs` to figure out how many processes to launch.

    batching : bool, default=False
        Whether to compare population members on small batches during
        evolution. Still uses full dataset for comparing against hall
        of fame.

    batch_size : int, default=50
        The amount of data to use if doing batching.

    fast_cycle : bool, default=False (experimental)
        Batch over population subsamples. This is a slightly different
        algorithm than regularized evolution, but does cycles 15%
        faster. May be algorithmically less efficient.

    precision : int, default=32
        What precision to use for the data. By default this is 32
        (float32), but you can select 64 or 16 as well.

    verbosity : int, default=1e9
        What verbosity level to use. 0 means minimal print statements.

    update_verbosity : int, default=None
        What verbosity level to use for package updates.
        Will take value of :param`verbosity` if not given.

    progress : bool, default=True
        Whether to use a progress bar instead of printing to stdout.

    equation_file : str, default=None
        Where to save the files (.csv separated by |).

    temp_equation_file :
        Whether to put the hall of fame file in the temp directory.
        Deletion is then controlled with the :param`delete_tempfiles`
        parameter.

    tempdir : str, default=None
        directory for the temporary files.

    delete_tempfiles : bool, default=True
        Whether to delete the temporary files after finishing.

    julia_project : str, default=None
        A Julia environment location containing a Project.toml
        (and potentially the source code for SymbolicRegression.jl).
        Default gives the Python package directory, where a
        Project.toml file should be present from the install.

    update: bool, default=True
        Whether to automatically update Julia packages.

    output_jax_format : bool, default=False
        Whether to create a 'jax_format' column in the output,
        containing jax-callable functions and the default parameters in
        a jax array.

    output_torch_format : bool, default=False
        Whether to create a 'torch_format' column in the output,
        containing a torch module with trainable parameters.

    extra_sympy_mappings : dict[str, Callable], default={}
        Provides mappings between custom :param`binary_operators` or
        :param`unary_operators` defined in julia strings, to those same
        operators defined in sympy.
        E.G if `unary_operators=["inv(x)=1/x"]`, then for the fitted
        model to be export to sympy, :param`extra_sympy_mappings`
        would be `{"inv": lambda x: 1/x}`.

    extra_jax_mappings : dict[Callable, str], default={}
        Similar to :param`extra_sympy_mappings` but for model export
        to jax. The dictionary maps sympy functions to jax functions.
        For example: `extra_jax_mappings={sympy.sin: "jnp.sin"}` maps
        the `sympy.sin` function to the equivalent jax expression `jnp.sin`.

    extra_torch_mappings : dict[Callable, Callable], default={}
        The same as :param`extra_jax_mappings` but for model export
        to pytorch. Note that the dictionary keys should be callable
        pytorch expressions.
        For example: `extra_torch_mappings={sympy.sin: torch.sin}`

    denoise : bool, default=False
        Whether to use a Gaussian Process to denoise the data before
        inputting to PySR. Can help PySR fit noisy data.

    select_k_features : int, default=None
         whether to run feature selection in Python using random forests,
         before passing to the symbolic regression code. None means no
         feature selection; an int means select that many features.

    kwargs : dict, default=None
        Supports deprecated keyword arguments. Other arguments will
        result in an error.

    Attributes
    ----------
    equations_ : pandas.DataFrame
        DataFrame containing the results of model fitting.

    n_features_in_ : int
        Number of features seen during :term:`fit`.

    feature_names_in_ : ndarray of shape (`n_features_in_`,)
        Names of features seen during :term:`fit`. Defined only when `X`
        has feature names that are all strings.

    nout_ : int
        Number of output dimensions.

    selection_mask_ : list[int] of length `select_k_features`
        List of indices for input features that are selected when
        :param`select_k_features` is set.

    raw_julia_state_ : tuple[list[PyCall.jlwrap], PyCall.jlwrap]
        The state for the julia SymbolicRegression.jl backend post fitting.

    Notes
    -----
    Most default parameters have been tuned over several example equations,
    but you should adjust `niterations`, `binary_operators`, `unary_operators`
    to your requirements. You can view more detailed explanations of the options
    on the [options page](https://astroautomata.com/PySR/#/options) of the
    documentation.

    Examples
    --------
    >>> import numpy as np
    >>> from pysr import PySRRegressor
    >>> randstate = np.random.RandomState(0)
    >>> X = 2 * randstate.randn(100, 5)
    >>> # y = 2.5372 * cos(x_3) + x_0 - 0.5
    >>> y = 2.5382 * np.cos(X[:, 3]) + X[:, 0] ** 2 - 0.5
    >>> model = PySRRegressor(
    >>>             niterations=40,
    >>>             binary_operators=["+", "*"],
    >>>             unary_operators=[
    >>>                 "cos",
    >>>                 "exp",
    >>>                 "sin",
    >>>                 "inv(x) = 1/x",  # Custom operator (julia syntax)
    >>>             ],
    >>>             model_selection="best",
    >>>             loss="loss(x, y) = (x - y)^2",  # Custom loss function (julia syntax)
    >>>         )
    >>> model.fit(X, y)
    PySRRegressor.equations = [0e-02  (((x0 * x0) + ((cos(x3) * inv(sin(sin(cos(0.5076455))))) * (0.79839814 + sin(inv(0.5623672))))) + -0.5378383)                                                                      pick      score                                           equation          loss  complexity
    0         0.000000                                          3.8552167  3.360272e+01           1
    1         1.189847                                          (x0 * x0)  3.110905e+00           3
    2         0.010626                          ((x0 * x0) + -0.25573406)  3.045491e+00           5
    3         0.896632                              (cos(x3) + (x0 * x0))  1.242382e+00           6
    4         0.811362                ((x0 * x0) + (cos(x3) * 2.4384754))  2.451971e-01           8
    5  >>>>  13.733371          (((cos(x3) * 2.5382) + (x0 * x0)) + -0.5)  2.889755e-13          10
    6         0.194695  ((x0 * x0) + (((cos(x3) + -0.063180044) * 2.53...  1.957723e-13          12
    7         0.006988  ((x0 * x0) + (((cos(x3) + -0.32505524) * 1.538...  1.944089e-13          13
    8         0.000955  (((((x0 * x0) + cos(x3)) + -0.8251649) + (cos(...  1.940381e-13          15
    ]
    >>> model.score(X, y)
    1.0
    >>> model.predict(np.array([1,2,3,4,5]))
    array([-1.15907818, -1.15907818, -1.15907818, -1.15907818, -1.15907818])
    """

    # Class validation constants
    VALID_OPTIMIZER_ALGORITHMS = ["NelderMead", "BFGS"]

    def __init__(
        self,
        model_selection="best",
        *,
        binary_operators=[
            "+",
            "-",
            "*",
            "/",
        ],
        unary_operators=[],
        niterations=40,
        populations=15,
        population_size=33,
        max_evals=None,
        maxsize=20,
        maxdepth=None,
        warmup_maxsize_by=0.0,
        timeout_in_seconds=None,
        constraints={},
        nested_constraints=None,
        loss="L2DistLoss()",
        complexity_of_operators=None,
        complexity_of_constants=1,
        complexity_of_variables=1,
        parsimony=0.0032,
        use_frequency=True,
        use_frequency_in_tournament=True,
        alpha=0.1,
        annealing=True,
        early_stop_condition=None,
        ncyclesperiteration=550,
        fraction_replaced=0.000364,
        fraction_replaced_hof=0.035,
        weight_add_node=0.79,
        weight_insert_node=5.1,
        weight_delete_node=1.7,
        weight_do_nothing=0.21,
        weight_mutate_constant=0.048,
        weight_mutate_operator=0.47,
        weight_randomize=0.00023,
        weight_simplify=0.0020,
        crossover_probability=0.066,
        skip_mutation_failures=True,
        migration=True,
        hof_migration=True,
        topn=12,
        should_optimize_constants=True,
        optimizer_algorithm="BFGS",
        optimizer_nrestarts=2,
        optimize_probability=0.14,
        optimizer_iterations=8,
        perturbation_factor=0.076,
        tournament_selection_n=10,
        tournament_selection_p=0.86,
        procs=cpu_count(),
        multithreading=None,
        cluster_manager=None,
        batching=False,
        batch_size=50,
        fast_cycle=False,
        precision=32,
        verbosity=1e9,
        update_verbosity=None,
        progress=True,
        equation_file=None,
        temp_equation_file=False,
        tempdir=None,
        delete_tempfiles=True,
        julia_project=None,
        update=True,
        output_jax_format=False,
        output_torch_format=False,
        extra_sympy_mappings={},
        extra_torch_mappings={},
        extra_jax_mappings={},
        denoise=False,
        select_k_features=None,
        **kwargs,
    ):

        # Hyperparameters
        # - Model search parameters
        self.model_selection = model_selection
        self.binary_operators = binary_operators
        self.unary_operators = unary_operators
        self.niterations = niterations
        self.populations = populations
        # - Model search Constraints
        self.population_size = population_size
        self.max_evals = max_evals
        self.maxsize = maxsize
        self.maxdepth = maxdepth
        self.warmup_maxsize_by = warmup_maxsize_by
        self.timeout_in_seconds = timeout_in_seconds
        self.constraints = constraints
        self.nested_constraints = nested_constraints
        # - Loss parameters
        self.loss = loss
        self.complexity_of_operators = complexity_of_operators
        self.complexity_of_constants = complexity_of_constants
        self.complexity_of_variables = complexity_of_variables
        self.parsimony = float(parsimony)
        self.use_frequency = use_frequency
        self.use_frequency_in_tournament = use_frequency_in_tournament
        self.alpha = alpha
        self.annealing = annealing
        self.early_stop_condition = early_stop_condition
        # - Evolutionary search parameters
        # -- Mutation parameters
        self.ncyclesperiteration = ncyclesperiteration
        self.fraction_replaced = fraction_replaced
        self.fraction_replaced_hof = fraction_replaced_hof
        self.weight_add_node = weight_add_node
        self.weight_insert_node = weight_insert_node
        self.weight_delete_node = weight_delete_node
        self.weight_do_nothing = weight_do_nothing
        self.weight_mutate_constant = weight_mutate_constant
        self.weight_mutate_operator = weight_mutate_operator
        self.weight_randomize = weight_randomize
        self.weight_simplify = weight_simplify
        self.crossover_probability = crossover_probability
        self.skip_mutation_failures = skip_mutation_failures
        # -- Migration parameters
        self.migration = migration
        self.hof_migration = hof_migration
        self.topn = topn
        # -- Constants parameters
        self.should_optimize_constants = should_optimize_constants
        self.optimizer_algorithm = optimizer_algorithm
        self.optimizer_nrestarts = optimizer_nrestarts
        self.optimize_probability = optimize_probability
        self.optimizer_iterations = optimizer_iterations
        self.perturbation_factor = perturbation_factor
        # -- Selection parameters
        self.tournament_selection_n = tournament_selection_n
        self.tournament_selection_p = tournament_selection_p
        # Solver parameters
        self.procs = procs
        self.multithreading = multithreading
        self.cluster_manager = cluster_manager
        self.batching = batching
        self.batch_size = batch_size
        self.fast_cycle = fast_cycle
        self.precision = precision
        # Additional runtime parameters
        # - Runtime user interface
        self.verbosity = verbosity
        self.update_verbosity = update_verbosity
        self.progress = progress
        # - Project management
        self.equation_file = equation_file
        self.temp_equation_file = temp_equation_file
        self.tempdir = tempdir
        self.delete_tempfiles = delete_tempfiles
        self.julia_project = julia_project
        self.update = update
        self.output_jax_format = output_jax_format
        self.output_torch_format = output_torch_format
        self.extra_sympy_mappings = extra_sympy_mappings
        self.extra_jax_mappings = extra_jax_mappings
        self.extra_torch_mappings = extra_torch_mappings
        # Pre-modelling transformation
        self.denoise = denoise
        self.select_k_features = select_k_features

        # Once all valid parameters have been assigned handle the
        # deprecated kwargs
        if len(kwargs) > 0:  # pragma: no cover
            deprecated_kwargs = make_deprecated_kwargs_for_pysr_regressor()
            for k, v in kwargs.items():
                # Handle renamed kwargs
                if k in deprecated_kwargs:
                    updated_kwarg_name = deprecated_kwargs[k]
                    setattr(self, updated_kwarg_name, v)
                    warnings.warn(
                        f"{k} has been renamed to {updated_kwarg_name} in PySRRegressor. "
                        f" Please use that instead.",
                        FutureWarning,
                    )
                # Handle kwargs that have been moved to the fit method
                elif k in ["weights", "variable_names", "Xresampled"]:
                    warnings.warn(
                        f"{k} is a data dependant parameter so should be passed when fit is called. "
                        f"Ignoring parameter; please pass {k} during the call to fit instead.",
                        FutureWarning,
                    )
                else:
                    raise TypeError(
                        f"{k} is not a valid keyword argument for PySRRegressor"
                    )

    def __repr__(self):
        """Prints all current equations fitted by the model.

        The string `>>>>` denotes which equation is selected by the
        `model_selection`.
        """
        if not hasattr(self, "equations_") or self.equations_ is None:
            return "PySRRegressor.equations_ = None"

        output = "PySRRegressor.equations_ = [\n"

        equations = self.equations_
        if not isinstance(equations, list):
            all_equations = [equations]
        else:
            all_equations = equations

        for i, equations in enumerate(all_equations):
            selected = ["" for _ in range(len(equations))]
            if self.model_selection == "accuracy":
                chosen_row = -1
            elif self.model_selection == "best":
                chosen_row = equations["score"].idxmax()
            else:
                raise NotImplementedError
            selected[chosen_row] = ">>>>"
            repr_equations = pd.DataFrame(
                dict(
                    pick=selected,
                    score=equations["score"],
                    equation=equations["equation"],
                    loss=equations["loss"],
                    complexity=equations["complexity"],
                )
            )

            if len(all_equations) > 1:
                output += "[\n"

            for line in repr_equations.__repr__().split("\n"):
                output += "\t" + line + "\n"

            if len(all_equations) > 1:
                output += "]"

            if i < len(all_equations) - 1:
                output += ", "

        output += "]"
        return output

    def get_best(self, index=None):
        """
        Get best equation using `model_selection`.

        Parameters
        ----------
        index : int, default=None
            If you wish to select a particular equation from `self.equations_`,
            give the row number here. This overrides the :param`model_selection`
            parameter.

        Returns
        -------
        best_equation : pandas.Series
            Dictionary representing the best expression found.

        Raises
        ------
        NotImplementedError
            Raised when an invalid model selection strategy is provided.
        """
        if self.equations_ is None:
            raise ValueError("No equations have been generated yet.")

        if index is not None:
            if isinstance(self.equations_, list):
                assert isinstance(index, list)
                return [eq.iloc[i] for eq, i in zip(self.equations_, index)]
            return self.equations_.iloc[index]

        if self.model_selection == "accuracy":
            if isinstance(self.equations_, list):
                return [eq.iloc[-1] for eq in self.equations_]
            return self.equations_.iloc[-1]
        elif self.model_selection == "best":
            if isinstance(self.equations_, list):
                return [eq.iloc[eq["score"].idxmax()] for eq in self.equations_]
            return self.equations_.iloc[self.equations_["score"].idxmax()]
        else:
            raise NotImplementedError(
                f"{self.model_selection} is not a valid model selection strategy."
            )

    def _validate_params(self, n_samples):
        """
        Perform validation on the parameters defined in init for the
        dataset specified in :term`fit`.

        Parameters
        ----------
        n_samples : int
            Number of samples in the dataset to be fitted.

        Returns
        -------
        self : object
            Reference to `self` with validated parameters.

        Raises
        ------
        ValueError
            Raised when on of the following occours: `tournament_selection_n`
            parameter is larger than `population_size`; `maxsize` is
            less than 7; invalid `extra_jax_mappings` or
            `extra_torch_mappings`; invalid optimizer algorithms.

        """
        # Handle None values for instance parameters:
        if self.multithreading is None:
            # Default is multithreading=True, unless explicitly set,
            # or procs is set to 0 (serial mode).
            self.multithreading = self.procs != 0 and self.cluster_manager is None
        if self.update_verbosity is None:
            self.update_verbosity = self.verbosity
        if self.maxdepth is None:
            self.maxdepth = self.maxsize

        # Cast tempdir string as a Path object
        self.tempdir_ = Path(tempfile.mkdtemp(dir=self.tempdir))
        if self.temp_equation_file:
            self.equation_file = self.tempdir_ / "hall_of_fame.csv"
        elif self.equation_file is None:
            date_time = datetime.now().strftime("%Y-%m-%d_%H%M%S.%f")[:-3]
            self.equation_file = "hall_of_fame_" + date_time + ".csv"

        # Handle type conversion for instance parameters:
        if isinstance(self.binary_operators, str):
            self.binary_operators = [self.binary_operators]
        if isinstance(self.unary_operators, str):
            self.unary_operators = [self.unary_operators]

        # Warn if instance parameters are not sensible values:
        if self.batch_size < 1:
            warnings.warn(
                "Given :param`batch_size` must be greater than or equal to one. "
                ":param`batch_size` has been increased to equal one."
            )
            self.batch_size = 1

        if n_samples > 10000 and not self.batching:
            warnings.warn(
                "Note: you are running with more than 10,000 datapoints. "
                "You should consider turning on batching (https://astroautomata.com/PySR/#/options?id=batching). "
                "You should also reconsider if you need that many datapoints. "
                "Unless you have a large amount of noise (in which case you "
                "should smooth your dataset first), generally < 10,000 datapoints "
                "is enough to find a functional form with symbolic regression. "
                "More datapoints will lower the search speed."
            )

        # Ensure instance parameters are allowable values:
        # ValueError - Incompatible values
        if self.tournament_selection_n > self.population_size:
            raise ValueError(
                "tournament_selection_n parameter must be smaller than population_size."
            )

        if self.maxsize > 40:
            warnings.warn(
                "Note: Using a large maxsize for the equation search will be exponentially slower and use significant memory. You should consider turning `use_frequency` to False, and perhaps use `warmup_maxsize_by`."
            )
        elif self.maxsize < 7:
            raise ValueError("PySR requires a maxsize of at least 7")

        if self.extra_jax_mappings is not None:
            for value in self.extra_jax_mappings.values():
                if not isinstance(value, str):
                    raise ValueError(
                        "extra_jax_mappings must have keys that are strings! e.g., {sympy.sqrt: 'jnp.sqrt'}."
                    )
        else:
            self.extra_jax_mappings = {}

        if self.extra_torch_mappings is not None:
            for value in self.extra_jax_mappings.values():
                if not callable(value):
                    raise ValueError(
                        "extra_torch_mappings must be callable functions! e.g., {sympy.sqrt: torch.sqrt}."
                    )
        else:
            self.extra_torch_mappings = {}

        # NotImplementedError - Values that could be supported at a later time
        if self.optimizer_algorithm not in self.VALID_OPTIMIZER_ALGORITHMS:
            raise NotImplementedError(
                f"PySR currently only supports the following optimizer algorithms: {self.VALID_OPTIMIZER_ALGORITHMS}"
            )

        # Handle presentation of the progress bar:
        buffer_available = "buffer" in sys.stdout.__dir__()
        if self.progress is not None:
            if self.progress and not buffer_available:
                warnings.warn(
                    "Note: it looks like you are running in Jupyter. The progress bar will be turned off."
                )
                self.progress = False
        else:
            self.progress = buffer_available

        return self

    def _validate_fit_params(self, X, y, Xresampled, variable_names):
        """
        Validates the parameters passed to the :term`fit` method.

        This method also setts the `nout_` attribute.

        Parameters
        ----------
        X : {ndarray | pandas.DataFrame} of shape (n_samples, n_features)
            Training data.

        y : {ndarray | pandas.DataFrame} of shape (n_samples,) or (n_samples, n_targets)
            Target values. Will be cast to X's dtype if necessary.

        Xresampled : {ndarray | pandas.DataFrame} of shape 
                        (n_resampled, n_features), default=None
            Resampled training data used for denoising.

        variable_names : list[str] of length n_features
            Names of each variable in the training dataset, `X`.

        Returns
        -------
        X_validated : ndarray of shape (n_samples, n_features)
            Validated training data.

        y_validated : ndarray of shape (n_samples,) or (n_samples, n_targets)
            Validatee target data.

        variable_names_validated : list[str] of length n_features
            Validated list of variable names for each feature in `X`.

        """
        if isinstance(X, pd.DataFrame):
            variable_names = None
            warnings.warn(
                ":param`variable_names` has been reset to `None` as `X` is a DataFrame. "
                "Will use DataFrame column names instead."
            )

            if X.columns.is_object() and X.columns.str.contains(" ").any():
                X.columns = X.columns.str.replace(" ", "_")
                warnings.warn(
                    "Spaces in DataFrame column names are not supported. "
                    "Spaces have been replaced with underscores. \n"
                    "Please rename the columns to valid names."
                )
        elif variable_names and [" " in name for name in variable_names].any():
            variable_names = [name.replace(" ", "_") for name in variable_names]
            warnings.warn(
                "Spaces in `variable_names` are not supported. "
                "Spaces have been replaced with underscores. \n"
                "Please use valid names instead."
            )
        # Only numpy values are needed from Xresampled, column metadata is
        # provided by X
        if isinstance(Xresampled, pd.DataFrame):
            Xresampled = Xresampled.values

        # Data validation and feature name fetching via sklearn
        # This method sets the n_features_in_ attribute
        X, y = self._validate_data(X=X, y=y, reset=True, multi_output=True)
        self.feature_names_in_ = _check_feature_names_in(self, variable_names)
        variable_names = self.feature_names_in_

        # Handle multioutput data
        if len(y.shape) == 1 or (len(y.shape) == 2 and y.shape[1] == 1):
            y = y.reshape(-1)
        elif len(y.shape) == 2:
            self.nout_ = y.shape[1]
        else:
            raise NotImplementedError("y shape not supported!")

        return X, y, variable_names

    def _pre_transform_training_data(self, X, y, Xresampled, variable_names):
        """
        Transforms the training data before fitting the symbolic regressor.

        This method also updates/sets the `selection_mask_` attribute.

        Parameters
        ----------
        X : {ndarray | pandas.DataFrame} of shape (n_samples, n_features)
            Training data.

        y : {ndarray | pandas.DataFrame} of shape (n_samples,) or (n_samples, n_targets)
            Target values. Will be cast to X's dtype if necessary.

        Xresampled : {ndarray | pandas.DataFrame} of shape 
                        (n_resampled, n_features), default=None
            Resampled training data used for denoising.

        variable_names : list[str] of length n_features
            Names of each variable in the training dataset, `X`.

        Returns
        -------
        X_transformed : ndarray of shape (n_samples, n_features)
            Transformed training data. n_samples will be equal to
            :param`Xresampled.shape[0]` if :param`self.denoise` is `True`,
            and :param`Xresampled is not None`, otherwise it will be
            equal to :param`X.shape[0]`. n_features will be equal to
            :param`self.select_k_features` if `self.select_k_features is not None`,
            otherwise it will be equal to :param`X.shape[1]`

        y_transformed : ndarray of shape (n_samples,) or  (n_samples, n_outputs)
            Transformed target data. n_samples will be equal to
            :param`Xresampled.shape[0]` if :param`self.denoise` is `True`,
            and :param`Xresampled is not None`, otherwise it will be
            equal to :param`X.shape[0]`.

        variable_names_transformed : list[str] of length n_features
            Names of each variable in the transformed dataset,
            `X_transformed`.
        """
        # Feature selection transformation
        if self.select_k_features:
            self.selection_mask_ = run_feature_selection(X, y, self.select_k_features)
            X = X[:, self.selection_mask_]

            if Xresampled is not None:
                Xresampled = Xresampled[:, self.selection_mask_]

            # Reduce variable_names to selection
            variable_names = [variable_names[i] for i in self.selection_mask_]

            # Re-perform data validation and feature name updating
            X, y = self._validate_data(
                X=X, y=y, reset=True, multi_output=True
            )
            # Update feature names with selected variable names
            self.feature_names_in_ = _check_feature_names_in(self, variable_names)
            print(f"Using features {self.feature_names_in_}")

        # Denoising transformation
        if self.denoise:
            if self.nout_ > 1:
                y = np.stack(
                    [
                        _denoise(X, y[:, i], Xresampled=Xresampled)[1]
                        for i in range(self.nout_)
                    ],
                    axis=1,
                )
                if Xresampled is not None:
                    X = Xresampled
            else:
                X, y = _denoise(X, y, Xresampled=Xresampled)

        return X, y, variable_names

    def _run(self, X, y, weights):
        """
        Run the symbolic regression fitting process on the julia backend.

        Parameters
        ----------
        X : {ndarray | pandas.DataFrame} of shape (n_samples, n_features)
            Training data.

        y : {ndarray | pandas.DataFrame} of shape (n_samples,) or (n_samples, n_targets)
            Target values. Will be cast to X's dtype if necessary.

        weights : {ndarray | pandas.DataFrame} of the same shape as y, default=None
            Each element is how to weight the mean-square-error loss
            for that particular element of y.

        Returns
        -------
        self : object
            Reference to `self` with fitted attributes.

        Raises
        ------
        ImportError
            Raised when the julia backend fails to import a package.
        """
        # Need to be global as we don't want to recreate/reinstate julia for 
        # every new instance of PySRRegressor
        global already_ran
        global Main

        # Start julia backend processes
        if Main is None:
            if self.multithreading:
                os.environ["JULIA_NUM_THREADS"] = str(self.procs)

            Main = init_julia()

        if self.cluster_manager is not None:
            Main.eval(f"import ClusterManagers: addprocs_{self.cluster_manager}")
            self.cluster_manager = Main.eval(f"addprocs_{self.cluster_manager}")

        self.julia_project, is_shared = _get_julia_project(self.julia_project)

        if not already_ran:
            Main.eval("using Pkg")
            io = "devnull" if self.update_verbosity == 0 else "stderr"
            io_arg = f"io={io}" if is_julia_version_greater_eq(Main, "1.6") else ""

            Main.eval(
                f'Pkg.activate("{_escape_filename(self.julia_project)}", shared = Bool({int(is_shared)}), {io_arg})'
            )
            from julia.api import JuliaError

            if is_shared:
                # Install SymbolicRegression.jl:
                _add_sr_to_julia_project(Main, io_arg)

            try:
                if self.update:
                    Main.eval(f"Pkg.resolve({io_arg})")
                    Main.eval(f"Pkg.instantiate({io_arg})")
                else:
                    Main.eval(f"Pkg.instantiate({io_arg})")
            except (JuliaError, RuntimeError) as e:
                raise ImportError(import_error_string(self.julia_project)) from e
            Main.eval("using SymbolicRegression")

            Main.plus = Main.eval("(+)")
            Main.sub = Main.eval("(-)")
            Main.mult = Main.eval("(*)")
            Main.pow = Main.eval("(^)")
            Main.div = Main.eval("(/)")

        _create_inline_operators(
            binary_operators=self.binary_operators, unary_operators=self.unary_operators
        )
        _handle_constraints(
            binary_operators=self.binary_operators,
            unary_operators=self.unary_operators,
            constraints=self.constraints,
        )

        una_constraints = [self.constraints[op] for op in self.unary_operators]
        bin_constraints = [self.constraints[op] for op in self.binary_operators]

        # Parse dict into Julia Dict for nested constraints::
        if self.nested_constraints is not None:
            nested_constraints_str = "Dict("
            for outer_k, outer_v in self.nested_constraints.items():
                nested_constraints_str += f"({outer_k}) => Dict("
                for inner_k, inner_v in outer_v.items():
                    nested_constraints_str += f"({inner_k}) => {inner_v}, "
                nested_constraints_str += "), "
            nested_constraints_str += ")"
            self.nested_constraints = Main.eval(nested_constraints_str)

        # Parse dict into Julia Dict for complexities:
        if self.complexity_of_operators is not None:
            complexity_of_operators_str = "Dict("
            for k, v in self.complexity_of_operators.items():
                complexity_of_operators_str += f"({k}) => {v}, "
            complexity_of_operators_str += ")"
            self.complexity_of_operators = Main.eval(complexity_of_operators_str)

        Main.custom_loss = Main.eval(self.loss)

        mutationWeights = [
            float(self.weight_mutate_constant),
            float(self.weight_mutate_operator),
            float(self.weight_add_node),
            float(self.weight_insert_node),
            float(self.weight_delete_node),
            float(self.weight_simplify),
            float(self.weight_randomize),
            float(self.weight_do_nothing),
        ]

        # Call to Julia backend.
        # See https://github.com/search?q=%22function+Options%22+repo%3AMilesCranmer%2FSymbolicRegression.jl+path%3A%2Fsrc%2F+filename%3AOptions.jl+language%3AJulia&type=Code
        options = Main.Options(
            binary_operators=Main.eval(
                str(tuple(self.binary_operators)).replace("'", "")
            ),
            unary_operators=Main.eval(
                str(tuple(self.unary_operators)).replace("'", "")
            ),
            bin_constraints=bin_constraints,
            una_constraints=una_constraints,
            complexity_of_operators=self.complexity_of_operators,
            complexity_of_constants=self.complexity_of_constants,
            complexity_of_variables=self.complexity_of_variables,
            nested_constraints=self.nested_constraints,
            loss=Main.custom_loss,
            maxsize=int(self.maxsize),
            hofFile=_escape_filename(self.equation_file),
            npopulations=int(self.populations),
            batching=self.batching,
            batchSize=int(min([self.batch_size, len(X)]) if self.batching else len(X)),
            mutationWeights=mutationWeights,
            probPickFirst=self.tournament_selection_p,
            ns=self.tournament_selection_n,
            # These have the same name:
            parsimony=self.parsimony,
            alpha=self.alpha,
            maxdepth=self.maxdepth,
            fast_cycle=self.fast_cycle,
            migration=self.migration,
            hofMigration=self.hof_migration,
            fractionReplacedHof=self.fraction_replaced_hof,
            shouldOptimizeConstants=self.should_optimize_constants,
            warmupMaxsizeBy=self.warmup_maxsize_by,
            useFrequency=self.use_frequency,
            useFrequencyInTournament=self.use_frequency_in_tournament,
            npop=self.population_size,
            ncyclesperiteration=self.ncyclesperiteration,
            fractionReplaced=self.fraction_replaced,
            topn=self.topn,
            verbosity=self.verbosity,
            optimizer_algorithm=self.optimizer_algorithm,
            optimizer_nrestarts=self.optimizer_nrestarts,
            optimize_probability=self.optimize_probability,
            optimizer_iterations=self.optimizer_iterations,
            perturbationFactor=self.perturbation_factor,
            annealing=self.annealing,
            stateReturn=True,  # Required for state saving.
            progress=self.progress,
            timeout_in_seconds=self.timeout_in_seconds,
            crossoverProbability=self.crossover_probability,
            skip_mutation_failures=self.skip_mutation_failures,
            max_evals=self.max_evals,
            earlyStopCondition=self.early_stop_condition,
        )

        # Convert data to desired precision
        np_dtype = {16: np.float16, 32: np.float32, 64: np.float64}[self.precision]

        Main.X = np.array(X, dtype=np_dtype).T
        if len(y.shape) == 1:
            Main.y = np.array(y, dtype=np_dtype)
        else:
            Main.y = np.array(y, dtype=np_dtype).T
        if weights is not None:
            if len(weights.shape) == 1:
                Main.weights = np.array(weights, dtype=np_dtype)
            else:
                Main.weights = np.array(weights, dtype=np_dtype).T
        else:
            Main.weights = None

        cprocs = 0 if self.multithreading else self.procs

        # Call to Julia backend.
        # See https://github.com/search?q=%22function+EquationSearch%22+repo%3AMilesCranmer%2FSymbolicRegression.jl+path%3A%2Fsrc%2F+filename%3ASymbolicRegression.jl+language%3AJulia&type=Code
        self.raw_julia_state_ = Main.EquationSearch(
            Main.X,
            Main.y,
            weights=Main.weights,
            niterations=int(self.niterations),
            varMap=self.feature_names_in_.tolist(),
            options=options,
            numprocs=int(cprocs),
            multithreading=bool(self.multithreading),
            saved_state=self.raw_julia_state_,
            addprocs_function=self.cluster_manager,
        )

        # Set attributes
        self.equations_ = self.get_hof()

        if self.delete_tempfiles:
            shutil.rmtree(self.tempdir_)

        already_ran = True

        return self

    def fit(
        self,
        X,
        y,
        Xresampled=None,
        weights=None,
        variable_names=None,
        from_equation_file=False,
    ):
        """
        Search for equations to fit the dataset and store them in `self.equations_`.

        Parameters
        ----------
        X : {ndarray | pandas.DataFrame} of shape (n_samples, n_features)
            Training data.

        y : {ndarray | pandas.DataFrame} of shape (n_samples,) or (n_samples, n_targets)
            Target values. Will be cast to X's dtype if necessary.

        Xresampled : {ndarray | pandas.DataFrame} of shape 
                        (n_resampled, n_features), default=None
            Resampled training data used for denoising.

        weights : {ndarray | pandas.DataFrame} of the same shape as y, default=None
            Each element is how to weight the mean-square-error loss
            for that particular element of y.

        variable_names : list[str], default=None
            A list of names for the variables, other than "x0", "x1", etc.
            If :param`X` is a pandas dataframe, the column names will be used.
            If variable_names are specified

        from_equation_file : bool, default=False
            Allows model to be initialized/fit from a previous run that has
            been saved to a file. If true, a value of y still needs to be
            passed such that `nout_` can be determined, however, the values of
            y are irrelevant and can be all zeros.

        Returns
        -------
        self : object
            Fitted Estimator.
        """

        # Init attributes that are not specified in BaseEstimator
        self.equations_ = None
        self.nout_ = 1
        self.selection_mask_ = None
        self.raw_julia_state_ = None

        # Parameter input validation (for parameters defined in __init__)
        self._validate_params(n_samples=X.shape[0])
        X, y, variable_names = self._validate_fit_params(
            X, y, Xresampled, variable_names
        )

        # Pre transformations (feature selection and denoising)
        X, y, variable_names = self._pre_transform_training_data(
            X, y, Xresampled, variable_names
        )

        # Warn about large feature counts (still warn if feature count is large 
        # after running feature selection)
        if self.n_features_in_ >= 10:
            warnings.warn(
                "Note: you are running with 10 features or more. "
                "Genetic algorithms like used in PySR scale poorly with large numbers of features. "
                "Consider using feature selection techniques to select the most important features "
                "(you can do this automatically with the `select_k_features` parameter), "
                "or, alternatively, doing a dimensionality reduction beforehand. "
                "For example, `X = PCA(n_components=6).fit_transform(X)`, "
                "using scikit-learn's `PCA` class, "
                "will reduce the number of features to 6 in an interpretable way, "
                "as each resultant feature "
                "will be a linear combination of the original features. "
            )

        # Assertion checks
        use_custom_variable_names = variable_names is not None
        # TODO: this is always true.

        _check_assertions(
            X,
            self.binary_operators,
            self.unary_operators,
            use_custom_variable_names,
            variable_names,
            weights,
            y,
        )

        # Fitting procedure
        if not from_equation_file:
            self._run(X=X, y=y, weights=weights)
        else:
            self.equations_ = self.get_hof()

        return self

    def refresh(self):
        """
        Updates self.equations_ with any new options passed, such as
        :param`extra_sympy_mappings`.
        """
        self.equations_ = self.get_hof()

    def _decision_function(self, X, best_equation):
        """
        Decide what value to predict based on the 'best' equation found
        from fitting.

        Parameters
        ----------
        X : {ndarray | pandas.DataFrame} of shape (n_samples, n_features)
            Testing data for evaluating the model.

        best_equation : pd.Series
            Selected best equation from `self.equations_`.

        Returns
        -------
        y_predicted : ndarray of shape (n_samples,) or (n_samples, nout_)
            Values predicted by substituting `X` into the
            :param`best_equation`.

        Raises
        ------
        ValueError
            Raises if the `best_equation` cannot be evaluated.
        """
        check_is_fitted(self)

        if isinstance(X, pd.DataFrame):
            X = X[self.feature_names_in_]
        elif self.selection_mask_ is not None:
            X = X[:, self.selection_mask_]

        X = self._validate_data(X, reset=False)
        try:
            if self.nout_ > 1:
                return np.stack(
                    [eq["lambda_format"](X) for eq in best_equation], axis=1
                )
            return best_equation["lambda_format"](X)
        except Exception as error:
            raise ValueError(
                "Failed to evaluate the expression. "
                "If you are using a custom operator, make sure to define it in :param`extra_sympy_mappings`, "
                "e.g., `model.set_params(extra_sympy_mappings={'inv': lambda x: 1 / x})`."
            ) from error

    def predict(self, X, index=None):
        """Predict y from input X using the equation chosen by `model_selection`.

        You may see what equation is used by printing this object. X should 
        have the same columns as the training data.

        Parameters
        ----------
        X : {ndarray | pandas.DataFrame} of shape (n_samples, n_features)
            Training data.

        index : int, default=None
            If you want to compute the output of an expression using a
            particular row of `self.equations_`, you may specify the index here.

        Returns
        -------
        y_predicted : ndarray of shape (n_samples, nout_)
            Values predicted by substituting `X` into the fitted symbolic
            regression model.
        """
        self.refresh()
        best_equation = self.get_best(index=index)
        return self._decision_function(X, best_equation)

    def sympy(self, index=None):
        """Return sympy representation of the equation(s) chosen by `model_selection`.

        Parameters
        ----------
        index : int, default=None
            If you wish to select a particular equation from
            `self.equations_`, give the index number here. This overrides
            the `model_selection` parameter.

        Returns
        -------
        best_equation : str, list[str] of length nout_
            SymPy representation of the best equation.
        """
        self.refresh()
        best_equation = self.get_best(index=index)
        if self.nout_ > 1:
            return [eq["sympy_format"] for eq in best_equation]
        return best_equation["sympy_format"]

    def latex(self, index=None):
        """Return latex representation of the equation(s) chosen by `model_selection`.

        Parameters
        ----------
        index : int, default=None
            If you wish to select a particular equation from
            `self.equations_`, give the index number here. This overrides
            the `model_selection` parameter.

        Returns
        -------
        best_equation : str or list[str] of length nout_
            LaTeX expression of the best equation.
        """
        self.refresh()
        sympy_representation = self.sympy(index=index)
        if self.nout_ > 1:
            return [sympy.latex(s) for s in sympy_representation]
        return sympy.latex(sympy_representation)

    def jax(self, index=None):
        """Return jax representation of the equation(s) chosen by `model_selection`.

        Each equation (multiple given if there are multiple outputs) is a dictionary
        containing {"callable": func, "parameters": params}. To call `func`, pass
        func(X, params). This function is differentiable using `jax.grad`.

        Parameters
        ----------
        index : int, default=None
            If you wish to select a particular equation from
            `self.equations_`, give the row number here. This overrides
            the `model_selection` parameter.

        Returns
        -------
        best_equation : dict[str, Any]
            Dictionary of callable jax function in "callable" key,
            and jax array of parameters as "parameters" key.
        """
        self.set_params(output_jax_format=True)
        self.refresh()
        best_equation = self.get_best(index=index)
        if self.nout_ > 1:
            return [eq["jax_format"] for eq in best_equation]
        return best_equation["jax_format"]

    def pytorch(self, index=None):
        """Return pytorch representation of the equation(s) chosen by `model_selection`.

        Each equation (multiple given if there are multiple outputs) is a PyTorch module
        containing the parameters as trainable attributes. You can use the module like
        any other PyTorch module: `module(X)`, where `X` is a tensor with the same
        column ordering as trained with.

        Parameters
        ----------
        index : int, default=None
            If you wish to select a particular equation from
            `self.equations_`, give the row number here. This overrides
            the `model_selection` parameter.

        Returns
        -------
        best_equation : torch.nn.Module
            PyTorch module representing the expression.
        """
        self.set_params(output_torch_format=True)
        self.refresh()
        best_equation = self.get_best(index=index)
        if self.nout_ > 1:
            return [eq["torch_format"] for eq in best_equation]
        return best_equation["torch_format"]

    def get_hof(self):
        """Get the equations from a hall of fame file. If no arguments
        entered, the ones used previously from a call to PySR will be used."""
        try:
            if self.nout_ > 1:
                all_outputs = []
                for i in range(1, self.nout_ + 1):
                    df = pd.read_csv(
                        str(self.equation_file) + f".out{i}" + ".bkup",
                        sep="|",
                    )
                    # Rename Complexity column to complexity:
                    df.rename(
                        columns={
                            "Complexity": "complexity",
                            "MSE": "loss",
                            "Equation": "equation",
                        },
                        inplace=True,
                    )

                    all_outputs.append(df)
            else:
                all_outputs = [pd.read_csv(str(self.equation_file) + ".bkup", sep="|")]
                all_outputs[-1].rename(
                    columns={
                        "Complexity": "complexity",
                        "MSE": "loss",
                        "Equation": "equation",
                    },
                    inplace=True,
                )
        except FileNotFoundError:
            raise RuntimeError(
                "Couldn't find equation file! The equation search likely exited before a single iteration completed."
            )

        ret_outputs = []

        for output in all_outputs:

            scores = []
            lastMSE = None
            lastComplexity = 0
            sympy_format = []
            lambda_format = []
            if self.output_jax_format:
                jax_format = []
            if self.output_torch_format:
                torch_format = []
            local_sympy_mappings = {
                **self.extra_sympy_mappings,
                **sympy_mappings,
            }

            sympy_symbols = [
                sympy.Symbol(self.feature_names_in_[i])
                for i in range(self.n_features_in_)
            ]

            for _, eqn_row in output.iterrows():
                eqn = sympify(eqn_row["equation"], locals=local_sympy_mappings)
                sympy_format.append(eqn)

                # Numpy:
                lambda_format.append(
                    CallableEquation(
                        sympy_symbols, eqn, self.selection_mask_, self.feature_names_in_
                    )
                )

                # JAX:
                if self.output_jax_format:
                    from .export_jax import sympy2jax

                    func, params = sympy2jax(
                        eqn,
                        sympy_symbols,
                        extra_jax_mappings=self.extra_jax_mappings,
                    )
                    jax_format.append({"callable": func, "parameters": params})

                # Torch:
                if self.output_torch_format:
                    from .export_torch import sympy2torch

                    module = sympy2torch(
                        eqn,
                        sympy_symbols,
                        extra_torch_mappings=self.extra_torch_mappings,
                    )
                    torch_format.append(module)

                curMSE = eqn_row["loss"]
                curComplexity = eqn_row["complexity"]

                if lastMSE is None:
                    cur_score = 0.0
                else:
                    if curMSE > 0.0:
                        cur_score = -np.log(curMSE / lastMSE) / (
                            curComplexity - lastComplexity
                        )
                    else:
                        cur_score = np.inf

                scores.append(cur_score)
                lastMSE = curMSE
                lastComplexity = curComplexity

            output["score"] = np.array(scores)
            output["sympy_format"] = sympy_format
            output["lambda_format"] = lambda_format
            output_cols = [
                "complexity",
                "loss",
                "score",
                "equation",
                "sympy_format",
                "lambda_format",
            ]
            if self.output_jax_format:
                output_cols += ["jax_format"]
                output["jax_format"] = jax_format
            if self.output_torch_format:
                output_cols += ["torch_format"]
                output["torch_format"] = torch_format

            ret_outputs.append(output[output_cols])

        if self.nout_ > 1:
            return ret_outputs
        return ret_outputs[0]


def _denoise(X, y, Xresampled=None):
    """Denoise the dataset using a Gaussian process"""
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, WhiteKernel, ConstantKernel

    gp_kernel = RBF(np.ones(X.shape[1])) + WhiteKernel(1e-1) + ConstantKernel()
    gpr = GaussianProcessRegressor(kernel=gp_kernel, n_restarts_optimizer=50)
    gpr.fit(X, y)
    if Xresampled is not None:
        return Xresampled, gpr.predict(Xresampled)

    return X, gpr.predict(X)


# Function hasnot been removed only due to usage in module tests
def _handle_feature_selection(X, select_k_features, y, variable_names):
    if select_k_features is not None:
        selection = run_feature_selection(X, y, select_k_features)
        print(f"Using features {[variable_names[i] for i in selection]}")
        X = X[:, selection]

    else:
        selection = None
    return X, selection


def run_feature_selection(X, y, select_k_features):
    """Use a gradient boosting tree regressor as a proxy for finding
    the k most important features in X, returning indices for those
    features as output."""
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.feature_selection import SelectFromModel

    clf = RandomForestRegressor(n_estimators=100, max_depth=3, random_state=0)
    clf.fit(X, y)
    selector = SelectFromModel(
        clf, threshold=-np.inf, max_features=select_k_features, prefit=True
    )
    return selector.get_support(indices=True)
