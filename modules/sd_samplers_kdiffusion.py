from collections import deque
import torch
import inspect
import k_diffusion.sampling
from modules import prompt_parser, devices, sd_samplers_common

from modules.shared import opts, state
import modules.shared as shared
from modules.script_callbacks import CFGDenoiserParams, cfg_denoiser_callback
from modules.script_callbacks import CFGDenoisedParams, cfg_denoised_callback
from modules.script_callbacks import AfterCFGCallbackParams, cfg_after_cfg_callback

samplers_k_diffusion = [
    ('Euler a', 'sample_euler_ancestral', ['k_euler_a', 'k_euler_ancestral'], {"uses_ensd": True}),
    ('Euler', 'sample_euler', ['k_euler'], {}),
    ('LMS', 'sample_lms', ['k_lms'], {}),
    ('Heun', 'sample_heun', ['k_heun'], {"second_order": True}),
    ('DPM2', 'sample_dpm_2', ['k_dpm_2'], {'discard_next_to_last_sigma': True}),
    ('DPM2 a', 'sample_dpm_2_ancestral', ['k_dpm_2_a'], {'discard_next_to_last_sigma': True, "uses_ensd": True}),
    ('DPM++ 2S a', 'sample_dpmpp_2s_ancestral', ['k_dpmpp_2s_a'], {"uses_ensd": True, "second_order": True}),
    ('DPM++ 2M', 'sample_dpmpp_2m', ['k_dpmpp_2m'], {}),
    ('DPM++ SDE', 'sample_dpmpp_sde', ['k_dpmpp_sde'], {"second_order": True, "brownian_noise": True}),
    ('DPM++ 2M SDE', 'sample_dpmpp_2m_sde', ['k_dpmpp_2m_sde_ka'], {"brownian_noise": True}),
    ('DPM fast', 'sample_dpm_fast', ['k_dpm_fast'], {"uses_ensd": True}),
    ('DPM adaptive', 'sample_dpm_adaptive', ['k_dpm_ad'], {"uses_ensd": True}),
    ('LMS Karras', 'sample_lms', ['k_lms_ka'], {'scheduler': 'karras'}),
    ('DPM2 Karras', 'sample_dpm_2', ['k_dpm_2_ka'], {'scheduler': 'karras', 'discard_next_to_last_sigma': True, "uses_ensd": True, "second_order": True}),
    ('DPM2 a Karras', 'sample_dpm_2_ancestral', ['k_dpm_2_a_ka'], {'scheduler': 'karras', 'discard_next_to_last_sigma': True, "uses_ensd": True, "second_order": True}),
    ('DPM++ 2S a Karras', 'sample_dpmpp_2s_ancestral', ['k_dpmpp_2s_a_ka'], {'scheduler': 'karras', "uses_ensd": True, "second_order": True}),
    ('DPM++ 2M Karras', 'sample_dpmpp_2m', ['k_dpmpp_2m_ka'], {'scheduler': 'karras'}),
    ('DPM++ SDE Karras', 'sample_dpmpp_sde', ['k_dpmpp_sde_ka'], {'scheduler': 'karras', "second_order": True, "brownian_noise": True}),
    ('DPM++ 2M SDE Karras', 'sample_dpmpp_2m_sde', ['k_dpmpp_2m_sde_ka'], {'scheduler': 'karras', "brownian_noise": True}),
]

samplers_data_k_diffusion = [
    sd_samplers_common.SamplerData(label, lambda model, funcname=funcname: KDiffusionSampler(funcname, model), aliases, options)
    for label, funcname, aliases, options in samplers_k_diffusion
    if hasattr(k_diffusion.sampling, funcname)
]

from tqdm.auto import trange

@torch.no_grad()
def sample_dpmpp_2m_alt(model, x, sigmas, extra_args=None, callback=None, disable=None):
    """DPM-Solver++(2M)."""
    extra_args = {} if extra_args is None else extra_args
    s_in = x.new_ones([x.shape[0]])
    sigma_fn = lambda t: t.neg().exp()
    t_fn = lambda sigma: sigma.log().neg()
    old_denoised = None

    for i in trange(len(sigmas) - 1, disable=disable):
        denoised = model(x, sigmas[i] * s_in, **extra_args)
        if callback is not None:
            callback({'x': x, 'i': i, 'sigma': sigmas[i], 'sigma_hat': sigmas[i], 'denoised': denoised})
        t, t_next = t_fn(sigmas[i]), t_fn(sigmas[i + 1])
        h = t_next - t
        if old_denoised is None or sigmas[i + 1] == 0:
            x = (sigma_fn(t_next) / sigma_fn(t)) * x - (-h).expm1() * denoised
        else:
            h_last = t - t_fn(sigmas[i - 1])
            r = h_last / h
            denoised_d = (1 + 1 / (2 * r)) * denoised - (1 / (2 * r)) * old_denoised
            x = (sigma_fn(t_next) / sigma_fn(t)) * x - (-h).expm1() * denoised_d
        sigma_progress = i / len(sigmas) 
        adjustment_factor = 1 + (0.15 * (sigma_progress * sigma_progress)) 
        old_denoised = denoised * adjustment_factor
    return x

k_diffusion.sampling.sample_dpmpp_2m_alt = sample_dpmpp_2m_alt

samplers_data_k_diffusion.insert(9, sd_samplers_common.SamplerData('DPM++ 2M alt', lambda model: KDiffusionSampler('sample_dpmpp_2m_alt', model), ['k_dpmpp_2m_alt'], {}))
samplers_data_k_diffusion.insert(10, sd_samplers_common.SamplerData('DPM++ 2M alt Karras', lambda model: KDiffusionSampler('sample_dpmpp_2m_alt', model), ['k_dpmpp_2m_alt_ka'], {'scheduler': 'karras'}))

sampler_extra_params = {
    'sample_euler': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
    'sample_heun': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
    'sample_dpm_2': ['s_churn', 's_tmin', 's_tmax', 's_noise'],
}

k_diffusion_samplers_map = {x.name: x for x in samplers_data_k_diffusion}
k_diffusion_scheduler = {
    'Automatic': None,
    'karras': k_diffusion.sampling.get_sigmas_karras,
    'exponential': k_diffusion.sampling.get_sigmas_exponential,
    'polyexponential': k_diffusion.sampling.get_sigmas_polyexponential
}


class CFGDenoiser(torch.nn.Module):
    """
    Classifier free guidance denoiser. A wrapper for stable diffusion model (specifically for unet)
    that can take a noisy picture and produce a noise-free picture using two guidances (prompts)
    instead of one. Originally, the second prompt is just an empty string, but we use non-empty
    negative prompt.
    """

    def __init__(self, model):
        super().__init__()
        self.inner_model = model
        self.mask = None
        self.nmask = None
        self.init_latent = None
        self.step = 0
        self.image_cfg_scale = None
        self.padded_cond_uncond = False

    def combine_denoised(self, x_out, conds_list, uncond, cond_scale):
        denoised_uncond = x_out[-uncond.shape[0]:]
        denoised = torch.clone(denoised_uncond)

        for i, conds in enumerate(conds_list):
            for cond_index, weight in conds:
                denoised[i] += (x_out[cond_index] - denoised_uncond[i]) * (weight * cond_scale)

        return denoised

    def combine_denoised_for_edit_model(self, x_out, cond_scale):
        out_cond, out_img_cond, out_uncond = x_out.chunk(3)
        denoised = out_uncond + cond_scale * (out_cond - out_img_cond) + self.image_cfg_scale * (out_img_cond - out_uncond)

        return denoised

    def forward(self, x, sigma, uncond, cond, cond_scale, s_min_uncond, image_cond):
        if state.interrupted or state.skipped:
            raise sd_samplers_common.InterruptedException

        # at self.image_cfg_scale == 1.0 produced results for edit model are the same as with normal sampling,
        # so is_edit_model is set to False to support AND composition.
        is_edit_model = shared.sd_model.cond_stage_key == "edit" and self.image_cfg_scale is not None and self.image_cfg_scale != 1.0

        conds_list, tensor = prompt_parser.reconstruct_multicond_batch(cond, self.step)
        uncond = prompt_parser.reconstruct_cond_batch(uncond, self.step)

        assert not is_edit_model or all(len(conds) == 1 for conds in conds_list), "AND is not supported for InstructPix2Pix checkpoint (unless using Image CFG scale = 1.0)"

        batch_size = len(conds_list)
        repeats = [len(conds_list[i]) for i in range(batch_size)]

        if shared.sd_model.model.conditioning_key == "crossattn-adm":
            image_uncond = torch.zeros_like(image_cond)
            make_condition_dict = lambda c_crossattn, c_adm: {"c_crossattn": c_crossattn, "c_adm": c_adm}
        else:
            image_uncond = image_cond
            make_condition_dict = lambda c_crossattn, c_concat: {"c_crossattn": c_crossattn, "c_concat": [c_concat]}

        if not is_edit_model:
            x_in = torch.cat([torch.stack([x[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [x])
            sigma_in = torch.cat([torch.stack([sigma[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [sigma])
            image_cond_in = torch.cat([torch.stack([image_cond[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [image_uncond])
        else:
            x_in = torch.cat([torch.stack([x[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [x] + [x])
            sigma_in = torch.cat([torch.stack([sigma[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [sigma] + [sigma])
            image_cond_in = torch.cat([torch.stack([image_cond[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [image_uncond] + [torch.zeros_like(self.init_latent)])

        denoiser_params = CFGDenoiserParams(x_in, image_cond_in, sigma_in, state.sampling_step, state.sampling_steps, tensor, uncond)
        cfg_denoiser_callback(denoiser_params)
        x_in = denoiser_params.x
        image_cond_in = denoiser_params.image_cond
        sigma_in = denoiser_params.sigma
        tensor = denoiser_params.text_cond
        uncond = denoiser_params.text_uncond
        skip_uncond = False

        # alternating uncond allows for higher thresholds without the quality loss normally expected from raising it
        if self.step % 2 and s_min_uncond > 0 and sigma[0] < s_min_uncond and not is_edit_model:
            skip_uncond = True
            x_in = x_in[:-batch_size]
            sigma_in = sigma_in[:-batch_size]

        self.padded_cond_uncond = False
        if shared.opts.pad_cond_uncond and tensor.shape[1] != uncond.shape[1]:
            empty = shared.sd_model.cond_stage_model_empty_prompt
            num_repeats = (tensor.shape[1] - uncond.shape[1]) // empty.shape[1]

            if num_repeats < 0:
                tensor = torch.cat([tensor, empty.repeat((tensor.shape[0], -num_repeats, 1))], axis=1)
                self.padded_cond_uncond = True
            elif num_repeats > 0:
                uncond = torch.cat([uncond, empty.repeat((uncond.shape[0], num_repeats, 1))], axis=1)
                self.padded_cond_uncond = True

        if tensor.shape[1] == uncond.shape[1] or skip_uncond:
            if is_edit_model:
                cond_in = torch.cat([tensor, uncond, uncond])
            elif skip_uncond:
                cond_in = tensor
            else:
                cond_in = torch.cat([tensor, uncond])

            if shared.batch_cond_uncond:
                x_out = self.inner_model(x_in, sigma_in, cond=make_condition_dict([cond_in], image_cond_in))
            else:
                x_out = torch.zeros_like(x_in)
                for batch_offset in range(0, x_out.shape[0], batch_size):
                    a = batch_offset
                    b = a + batch_size
                    x_out[a:b] = self.inner_model(x_in[a:b], sigma_in[a:b], cond=make_condition_dict([cond_in[a:b]], image_cond_in[a:b]))
        else:
            x_out = torch.zeros_like(x_in)
            batch_size = batch_size*2 if shared.batch_cond_uncond else batch_size
            for batch_offset in range(0, tensor.shape[0], batch_size):
                a = batch_offset
                b = min(a + batch_size, tensor.shape[0])

                if not is_edit_model:
                    c_crossattn = [tensor[a:b]]
                else:
                    c_crossattn = torch.cat([tensor[a:b]], uncond)

                x_out[a:b] = self.inner_model(x_in[a:b], sigma_in[a:b], cond=make_condition_dict(c_crossattn, image_cond_in[a:b]))

            if not skip_uncond:
                x_out[-uncond.shape[0]:] = self.inner_model(x_in[-uncond.shape[0]:], sigma_in[-uncond.shape[0]:], cond=make_condition_dict([uncond], image_cond_in[-uncond.shape[0]:]))

        denoised_image_indexes = [x[0][0] for x in conds_list]
        if skip_uncond:
            fake_uncond = torch.cat([x_out[i:i+1] for i in denoised_image_indexes])
            x_out = torch.cat([x_out, fake_uncond])  # we skipped uncond denoising, so we put cond-denoised image to where the uncond-denoised image should be

        denoised_params = CFGDenoisedParams(x_out, state.sampling_step, state.sampling_steps, self.inner_model)
        cfg_denoised_callback(denoised_params)

        devices.test_for_nans(x_out, "unet")

        if opts.live_preview_content == "Prompt":
            sd_samplers_common.store_latent(torch.cat([x_out[i:i+1] for i in denoised_image_indexes]))
        elif opts.live_preview_content == "Negative prompt":
            sd_samplers_common.store_latent(x_out[-uncond.shape[0]:])

        if is_edit_model:
            denoised = self.combine_denoised_for_edit_model(x_out, cond_scale)
        elif skip_uncond:
            denoised = self.combine_denoised(x_out, conds_list, uncond, 1.0)
        else:
            denoised = self.combine_denoised(x_out, conds_list, uncond, cond_scale)

        if self.mask is not None:
            denoised = self.init_latent * self.mask + self.nmask * denoised

        after_cfg_callback_params = AfterCFGCallbackParams(denoised, state.sampling_step, state.sampling_steps)
        cfg_after_cfg_callback(after_cfg_callback_params)
        denoised = after_cfg_callback_params.x

        self.step += 1
        return denoised


class TorchHijack:
    def __init__(self, sampler_noises):
        # Using a deque to efficiently receive the sampler_noises in the same order as the previous index-based
        # implementation.
        self.sampler_noises = deque(sampler_noises)

    def __getattr__(self, item):
        if item == 'randn_like':
            return self.randn_like

        if hasattr(torch, item):
            return getattr(torch, item)

        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{item}'")

    def randn_like(self, x):
        if self.sampler_noises:
            noise = self.sampler_noises.popleft()
            if noise.shape == x.shape:
                return noise

        if opts.randn_source == "CPU" or x.device.type == 'mps':
            return torch.randn_like(x, device=devices.cpu).to(x.device)
        else:
            return torch.randn_like(x)


class KDiffusionSampler:
    def __init__(self, funcname, sd_model):
        denoiser = k_diffusion.external.CompVisVDenoiser if sd_model.parameterization == "v" else k_diffusion.external.CompVisDenoiser

        self.model_wrap = denoiser(sd_model, quantize=shared.opts.enable_quantization)
        self.funcname = funcname
        self.func = getattr(k_diffusion.sampling, self.funcname)
        self.extra_params = sampler_extra_params.get(funcname, [])
        self.model_wrap_cfg = CFGDenoiser(self.model_wrap)
        self.sampler_noises = None
        self.stop_at = None
        self.eta = None
        self.config = None  # set by the function calling the constructor
        self.last_latent = None
        self.s_min_uncond = None

        self.conditioning_key = sd_model.model.conditioning_key

    def callback_state(self, d):
        step = d['i']
        latent = d["denoised"]
        if opts.live_preview_content == "Combined":
            sd_samplers_common.store_latent(latent)
        self.last_latent = latent

        if self.stop_at is not None and step > self.stop_at:
            raise sd_samplers_common.InterruptedException

        state.sampling_step = step
        shared.total_tqdm.update()

    def launch_sampling(self, steps, func):
        state.sampling_steps = steps
        state.sampling_step = 0

        try:
            return func()
        except RecursionError:
            print(
                'Encountered RecursionError during sampling, returning last latent. '
                'rho >5 with a polyexponential scheduler may cause this error. '
                'You should try to use a smaller rho value instead.'
            )
            return self.last_latent
        except sd_samplers_common.InterruptedException:
            return self.last_latent

    def number_of_needed_noises(self, p):
        return p.steps

    def initialize(self, p):
        self.model_wrap_cfg.mask = p.mask if hasattr(p, 'mask') else None
        self.model_wrap_cfg.nmask = p.nmask if hasattr(p, 'nmask') else None
        self.model_wrap_cfg.step = 0
        self.model_wrap_cfg.image_cfg_scale = getattr(p, 'image_cfg_scale', None)
        self.eta = p.eta if p.eta is not None else opts.eta_ancestral
        self.s_min_uncond = getattr(p, 's_min_uncond', 0.0)

        k_diffusion.sampling.torch = TorchHijack(self.sampler_noises if self.sampler_noises is not None else [])

        extra_params_kwargs = {}
        for param_name in self.extra_params:
            if hasattr(p, param_name) and param_name in inspect.signature(self.func).parameters:
                extra_params_kwargs[param_name] = getattr(p, param_name)

        if 'eta' in inspect.signature(self.func).parameters:
            if self.eta != 1.0:
                p.extra_generation_params["Eta"] = self.eta

            extra_params_kwargs['eta'] = self.eta

        return extra_params_kwargs

    def get_sigmas(self, p, steps):
        discard_next_to_last_sigma = self.config is not None and self.config.options.get('discard_next_to_last_sigma', False)
        if opts.always_discard_next_to_last_sigma and not discard_next_to_last_sigma:
            discard_next_to_last_sigma = True
            p.extra_generation_params["Discard penultimate sigma"] = True

        steps += 1 if discard_next_to_last_sigma else 0

        if p.sampler_noise_scheduler_override:
            sigmas = p.sampler_noise_scheduler_override(steps)
        elif opts.k_sched_type != "Automatic":
            m_sigma_min, m_sigma_max = (self.model_wrap.sigmas[0].item(), self.model_wrap.sigmas[-1].item())
            sigma_min, sigma_max = (0.1, 10) if opts.use_old_karras_scheduler_sigmas else (m_sigma_min, m_sigma_max)
            sigmas_kwargs = {
                'sigma_min': sigma_min,
                'sigma_max': sigma_max,
            }

            sigmas_func = k_diffusion_scheduler[opts.k_sched_type]
            p.extra_generation_params["Schedule type"] = opts.k_sched_type

            if opts.sigma_min != m_sigma_min and opts.sigma_min != 0:
                sigmas_kwargs['sigma_min'] = opts.sigma_min
                p.extra_generation_params["Schedule min sigma"] = opts.sigma_min
            if opts.sigma_max != m_sigma_max and opts.sigma_max != 0:
                sigmas_kwargs['sigma_max'] = opts.sigma_max
                p.extra_generation_params["Schedule max sigma"] = opts.sigma_max

            default_rho = 1. if opts.k_sched_type == "polyexponential" else 7.

            if opts.k_sched_type != 'exponential' and opts.rho != 0 and opts.rho != default_rho:
                sigmas_kwargs['rho'] = opts.rho
                p.extra_generation_params["Schedule rho"] = opts.rho

            sigmas = sigmas_func(n=steps, **sigmas_kwargs, device=shared.device)
        elif self.config is not None and self.config.options.get('scheduler', None) == 'karras':
            sigma_min, sigma_max = (0.1, 10) if opts.use_old_karras_scheduler_sigmas else (self.model_wrap.sigmas[0].item(), self.model_wrap.sigmas[-1].item())

            sigmas = k_diffusion.sampling.get_sigmas_karras(n=steps, sigma_min=sigma_min, sigma_max=sigma_max, device=shared.device)
        else:
            sigmas = self.model_wrap.get_sigmas(steps)

        if discard_next_to_last_sigma:
            sigmas = torch.cat([sigmas[:-2], sigmas[-1:]])

        return sigmas

    def create_noise_sampler(self, x, sigmas, p):
        """For DPM++ SDE: manually create noise sampler to enable deterministic results across different batch sizes"""
        if shared.opts.no_dpmpp_sde_batch_determinism:
            return None

        from k_diffusion.sampling import BrownianTreeNoiseSampler
        sigma_min, sigma_max = sigmas[sigmas > 0].min(), sigmas.max()
        current_iter_seeds = p.all_seeds[p.iteration * p.batch_size:(p.iteration + 1) * p.batch_size]
        return BrownianTreeNoiseSampler(x, sigma_min, sigma_max, seed=current_iter_seeds)

    def sample_img2img(self, p, x, noise, conditioning, unconditional_conditioning, steps=None, image_conditioning=None):
        steps, t_enc = sd_samplers_common.setup_img2img_steps(p, steps)

        sigmas = self.get_sigmas(p, steps)

        sigma_sched = sigmas[steps - t_enc - 1:]
        xi = x + noise * sigma_sched[0]

        extra_params_kwargs = self.initialize(p)
        parameters = inspect.signature(self.func).parameters

        if 'sigma_min' in parameters:
            ## last sigma is zero which isn't allowed by DPM Fast & Adaptive so taking value before last
            extra_params_kwargs['sigma_min'] = sigma_sched[-2]
        if 'sigma_max' in parameters:
            extra_params_kwargs['sigma_max'] = sigma_sched[0]
        if 'n' in parameters:
            extra_params_kwargs['n'] = len(sigma_sched) - 1
        if 'sigma_sched' in parameters:
            extra_params_kwargs['sigma_sched'] = sigma_sched
        if 'sigmas' in parameters:
            extra_params_kwargs['sigmas'] = sigma_sched

        if self.config.options.get('brownian_noise', False):
            noise_sampler = self.create_noise_sampler(x, sigmas, p)
            extra_params_kwargs['noise_sampler'] = noise_sampler

        self.model_wrap_cfg.init_latent = x
        self.last_latent = x
        extra_args = {
            'cond': conditioning,
            'image_cond': image_conditioning,
            'uncond': unconditional_conditioning,
            'cond_scale': p.cfg_scale,
            's_min_uncond': self.s_min_uncond
        }

        samples = self.launch_sampling(t_enc + 1, lambda: self.func(self.model_wrap_cfg, xi, extra_args=extra_args, disable=False, callback=self.callback_state, **extra_params_kwargs))

        if self.model_wrap_cfg.padded_cond_uncond:
            p.extra_generation_params["Pad conds"] = True

        return samples

    def sample(self, p, x, conditioning, unconditional_conditioning, steps=None, image_conditioning=None):
        steps = steps or p.steps

        sigmas = self.get_sigmas(p, steps)

        x = x * sigmas[0]

        extra_params_kwargs = self.initialize(p)
        parameters = inspect.signature(self.func).parameters

        if 'sigma_min' in parameters:
            extra_params_kwargs['sigma_min'] = self.model_wrap.sigmas[0].item()
            extra_params_kwargs['sigma_max'] = self.model_wrap.sigmas[-1].item()
            if 'n' in parameters:
                extra_params_kwargs['n'] = steps
        else:
            extra_params_kwargs['sigmas'] = sigmas

        if self.config.options.get('brownian_noise', False):
            noise_sampler = self.create_noise_sampler(x, sigmas, p)
            extra_params_kwargs['noise_sampler'] = noise_sampler

        self.last_latent = x
        samples = self.launch_sampling(steps, lambda: self.func(self.model_wrap_cfg, x, extra_args={
            'cond': conditioning,
            'image_cond': image_conditioning,
            'uncond': unconditional_conditioning,
            'cond_scale': p.cfg_scale,
            's_min_uncond': self.s_min_uncond
        }, disable=False, callback=self.callback_state, **extra_params_kwargs))

        if self.model_wrap_cfg.padded_cond_uncond:
            p.extra_generation_params["Pad conds"] = True

        return samples

