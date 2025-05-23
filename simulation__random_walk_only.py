# mcandrew

import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from scipy.interpolate import interp1d

import scienceplots

class compartment_forecast_with_GP(object):
    # Initialize the forecasting framework
    def __init__(self
                 , N=None                 # Total population
                 , y=None                 # Observed incident cases (with missing values)
                 , X=None                 # Covariate matrix for GP kernel (e.g. time or other predictors)
                 , times=None             # Array of time points
                 , start=None, end=None   # Start and end time (used if times is None)
                 , infectious_period=None):  # Fixed infectious period (used to derive gamma)
        
        self.N = N
        self.times = times
        self.infectious_period = infectious_period

        # Set time boundaries
        if times is not None:
            self.start = min(times)
            self.end   = max(times)
        else:
            self.start, self.end = start, end

        self.y = y

        # Find first missing value in y to determine training length
        if y is not None:
            self.nobs = np.min(np.argwhere(np.isnan(y)))
        else:
            self.nobs = None

        self.X = X

    # Simulate epidemic trajectories with a stochastic SIR model
    def simulation(self, I0=None, repo=None, dt=1./7):
        import numpy as np

        N = self.N
        infectious_period = self.infectious_period
        start, end = self.start, self.end
        gamma = 1. / infectious_period

        S0, I0, R0, i0 = N - I0, I0, 0, I0
        y = [(S0, I0, R0, i0)]

        # Time grid for simulation
        times = np.linspace(start, end, (end - start) * int(1. / dt))

        for t in times:
            S, I, R, i = y[-1]
            beta = repo * gamma

            # Simulate infections and recoveries using Poisson noise
            infection = np.random.poisson(dt * (beta * S * I / N))
            recover = np.random.poisson(dt * (gamma * I))

            # Update compartments (clipped to [0, N])
            S = np.clip(S - infection, 0, N)
            I = np.clip(I + infection - recover, 0, N)
            R = np.clip(R + recover, 0, N)
            i += infection

            y.append((S, I, R, i))

        S, I, R, i = zip(*y)
        i = np.diff(i)  # Daily incident cases

        return times, i, y

    # Fit model to control scenario using NumPyro and GP residuals
    def control_fit(self, dt=1./7):
        import jax
        import jax.numpy as jnp

        import numpyro
        import numpyro.distributions as dist
        from numpyro.infer import MCMC, NUTS
        from diffrax import diffeqsolve, ODETerm, Heun, SaveAt

        def model(y=None, times=None, N=None):
            #--setup residual vector
            nobs = self.nobs

            # Define RBF kernel (optional for multiple covariates)
            def rbf_kernel_ard(X1, X2, amplitude, lengthscales):
                X1_scaled = X1 / lengthscales
                X2_scaled = X2 / lengthscales
                dists = jnp.sum((X1_scaled[:, None, :] - X2_scaled[None, :, :])**2, axis=-1)
                return amplitude**2 * jnp.exp(-0.5 * dists)

            def random_walk_kernel(X, X2=None, variance=1.0):
                if X2 is None:
                    X2 = X
                return variance * jnp.minimum(X, X2.T)

            noise      = numpyro.sample("noise", dist.HalfCauchy(1.))
            sigma_obs  = numpyro.sample("sigma_obs", dist.HalfCauchy(1.))
            
            ncols  = X.shape[-1]
            rw_var = numpyro.sample("rw_var", dist.HalfCauchy(1.))
            K1     = random_walk_kernel(X[:, 0].reshape(-1, 1), variance=rw_var)

            # Optionally add RBF kernel if extra features exist
            if ncols > 1:
                amp  = numpyro.sample("amp", dist.Beta(1., 1.))
                leng = numpyro.sample("leng", dist.HalfCauchy(1.))
                K2   = rbf_kernel_ard(X[:, 1:], X[:, 1:], amp, leng)
                K    = K1 + K2
            else:
                K = K1

            # Compute submatrices for GP residual conditioning
            KOO = K[:nobs, :nobs] + noise * jnp.eye(nobs)
            KTT = K[nobs:, nobs:]
            KOT = K[:nobs, nobs:]

            center         = jnp.nanmean(y)
            centered_y     = (y-center)[:nobs]
            
            # Poisson observation model on residual-corrected prediction
            numpyro.sample("likelihood",
                           dist.MultivariateNormal(0,covariance_matrix=KOO),
                           obs=centered_y)

            # Compute conditional GP mean and covariance
            L     = jnp.linalg.cholesky(KOO + 1e-5 * jnp.eye(nobs))
            alpha = jax.scipy.linalg.solve_triangular(L, centered_y, lower=True)
            alpha = jax.scipy.linalg.solve_triangular(L.T, alpha, lower=False)

            mean = KOT.T @ alpha

            v = jax.scipy.linalg.solve_triangular(L, KOT, lower=True)
            cov = KTT - v.T @ v

            fitted_resid = numpyro.sample("fitted_resid", dist.MultivariateNormal(mean, covariance_matrix=cov))
            final_resid  = jnp.concatenate([y[:nobs], fitted_resid + center ]) 

            yhat = numpyro.deterministic("yhat", final_resid)

        # Run MCMC with NUTS sampler
        mcmc = MCMC(NUTS(model, max_tree_depth=3), num_warmup=5000, num_samples=5000)
        mcmc.run(jax.random.PRNGKey(1), y=jnp.array(self.y), times=jnp.array(self.times), N=self.N)

        mcmc.print_summary()
        samples = mcmc.get_samples()
        incs    = samples["yhat"]

        # Generate posterior predictive samples using previously drawn MCMC samples
        from numpyro.infer import Predictive

        # Define model as used in control_fit (reusing trace)
        predictive = Predictive(model
                                ,posterior_samples=samples
                                ,return_sites=["yhat"])

        preds = predictive(jax.random.PRNGKey(2)
                           ,y     = jnp.array(self.y)
                           ,times = jnp.array(self.times)
                           ,N     = self.N)


        yhats = preds["yhat"]
        
        self.samples = samples
        return times, yhats, samples
    
if __name__ == "__main__":

    np.random.seed(1010)

    #--this simulation is at a daily temporal scale
    framework = compartment_forecast_with_GP(N                   = 1000
                                             , start             = 0
                                             , end               = 32 
                                             , infectious_period = 2)
    times, infections, all_states = framework.simulation(I0=5,repo=2)

    #--aggregating up to week temporal scale
    weeks              = np.arange(0,32)
    weekly_infections  = infections.reshape(32,-1).sum(-1)

    #--lets further assume we know only the first 10 time units
    full_weekly_infections = weekly_infections
    
    weekly_infections = np.array([ float(x) for x in weekly_infections])
    weekly_infections[10:] = np.nan

    #--time paraemters
    start,end = min(weeks), max(weeks)+1

    #--Control model only uses X = time in the kernel
    X     = np.arange( 1,end+1 ).reshape(-1,1)
    X     = np.hstack([X,X]) 

    #--model fit for control
    framework = compartment_forecast_with_GP(N       = 1000
                                             , times = weeks
                                             , y     = weekly_infections
                                             , X     = X
                                             , infectious_period = 2)
 
    times,infections,samples = framework.control_fit()

    

    colors = sns.color_palette("tab10",2)
    plt.style.use("science")
    
    fig, ax = plt.subplots()
    ax.scatter(weeks, full_weekly_infections,s=8, color="black")
    ax.set_xlabel("MMWR week", fontsize=8)
    ax.set_ylabel("Incident cases", fontsize=8)

    ax.axvline(9,color="black",ls="--")
    
    lower1,lower2,middle,upper2,upper1 = np.percentile(infections,[2.5,25,50,75,97.5],axis=0)
    ax.fill_between(weeks,lower1,upper1,alpha=0.2       ,color=colors[0])
    ax.fill_between(weeks,lower1,upper1,alpha=0.2       ,color=colors[0])
    ax.plot(        weeks,middle                 ,lw=1.5, color=colors[0])
    
    plt.show()
