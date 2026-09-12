"""Fixed 0.153.4 registry controls; these do not establish OS/provider readiness."""

import json

# Snapshot used by the accepted GPT-5.5 macOS/Windows empty-environment probes.
_DISABLED_FEATURES = """
apply_patch_preserve_line_endings apply_patch_streaming_events apps artifact auth_elicitation
background_paginated_rollout_migration bedrock_setup_wizard browser_use browser_use_external
browser_use_full_cdp_access chronicle code_mode code_mode_host code_mode_interrupt
code_mode_only code_mode_prewarm compaction_image_budget computer_use
concurrent_reasoning_summaries content_item_kinds context_management current_time_reminder
cwd_relative_turn_diffs default_mode_request_user_input deferred_executor
deferred_tool_world_state enable_mcp_apps enable_request_compression exec_permission_approvals
executed_tool_call_metadata executor_capability_discovery external_agent_memory_import fast_mode
goals guardian_approval guardian_enhanced_node_repl_transcripts guardian_ext
guardian_node_repl_transcript_images guardian_reuse_parent_compaction guardianv2 hooks
image_generation image_resize_notice in_app_browser in_app_chat in_app_dictation
in_app_local_automation in_app_updates local_thread_store_compression mcp_2026_07_28
mcp_oauth_refresh_coordination memories mentions_v2 multi_agent multi_agent_v2 network_proxy
non_prefixed_mcp_tool_names omit_app_server_notification_media personality plugin_sharing
plugins powershell_shell_version prevent_idle_sleep psp realtime_conversation
recommended_plugins remote_compaction_v2 remote_plugin request_permissions_tool
respect_system_proxy retain_client_developer_messages rollout_budget runtime_metrics
secret_auth_storage shell_snapshot shell_snapshot_v2 shell_tool shell_zsh_fork
skill_mcp_dependency_install skill_search skip_host_skill_discovery sleep_tool
standalone_web_search step_model_switching terminal_visualization_instructions token_budget
tool_call_mcp_elicitation tool_suggest transcript_v2 unbounded_connection_retries unified_exec
unified_image_budget use_agent_identity use_legacy_landlock view_image web_search_cached
web_search_request workspace_dependencies write_stdin_approval
""".split()


def policy_args() -> list[str]:
    config: dict[str, str | bool] = {
        "model_provider": "openai",
        "cli_auth_credentials_store": "file",
        "sandbox_mode": "read-only",
        "approval_policy": "untrusted",
        # Never silently select the weaker Windows unelevated backend.
        "windows.sandbox": "elevated",
        "analytics.enabled": False,
        "web_search": "disabled",
        "apps._default.enabled": False,
        "tools.experimental_request_user_input.enabled": False,
        "orchestrator.skills.enabled": False,
        "orchestrator.mcp.enabled": False,
        **{f"features.{name}": False for name in _DISABLED_FEATURES},
    }
    return [
        "--strict-config",
        *(arg for key, value in config.items() for arg in ("-c", f"{key}={json.dumps(value)}")),
    ]
