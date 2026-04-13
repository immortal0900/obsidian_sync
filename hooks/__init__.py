from __future__ import annotations


def load_hooks(hook_names: list[str], **kwargs) -> list:
    """
    Instantiate hooks by name. kwargs are passed to each hook constructor.
    Raises KeyError with a descriptive message if a hook name is unknown.
    Import is deferred to avoid circular imports at module load time.
    """
    from hooks.sync_hook import SyncHook

    HOOK_REGISTRY: dict[str, type] = {
        "sync": SyncHook,
        # Phase 2: "blog_convert": BlogConvertHook,
        # Phase 3: "llm_tagging": LLMTaggingHook,
    }

    hooks = []
    for name in hook_names:
        if name not in HOOK_REGISTRY:
            raise KeyError(
                f"Unknown hook: '{name}'. Available: {list(HOOK_REGISTRY)}"
            )
        hooks.append(HOOK_REGISTRY[name](**kwargs))
    return hooks
