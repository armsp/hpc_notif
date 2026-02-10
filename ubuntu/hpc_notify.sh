#!/usr/bin/env bash
# ============================================================================
# hpc_notify.sh — Source this in your SLURM/PBS job scripts
#
# Setup:
#   1. Copy this file to your HPC home directory
#   2. Set NTFY_TOPIC below (or export it in your .bashrc)
#   3. In job scripts:  source ~/hpc_notify.sh
# ============================================================================

# ---- CONFIGURE THIS (or export NTFY_TOPIC in your .bashrc) ----
NTFY_TOPIC="${NTFY_TOPIC:-YOUR_TOPIC_HERE}"
NTFY_SERVER="${NTFY_SERVER:-https://ntfy.sh}"
# ----------------------------------------------------------------

# Send a notification
# Usage: hpc_notify "message" [priority] [title]
#   priority: 1=min, 2=low, 3=default, 4=high, 5=urgent
hpc_notify() {
    local message="${1:-No message}"
    local priority="${2:-3}"
    local title="${3:-HPC Job}"

    if [ -n "$SLURM_JOB_ID" ]; then
        title="Job $SLURM_JOB_ID ($SLURM_JOB_NAME)"
    fi

    curl -s \
        -H "Title: $title" \
        -H "Priority: $priority" \
        -d "$message" \
        "$NTFY_SERVER/$NTFY_TOPIC" > /dev/null 2>&1
}

# Wrapper: auto-notify on start, finish, or crash
# Usage: hpc_run <command> [args...]
hpc_run() {
    local cmd="$*"
    local job_label="${SLURM_JOB_ID:+Job $SLURM_JOB_ID — }"

    hpc_notify "🚀 Started: ${job_label}${cmd}" 3
    local start_time=$SECONDS

    "$@"
    local exit_code=$?

    local elapsed=$(( SECONDS - start_time ))
    local duration=$(printf '%dh %dm %ds' $((elapsed/3600)) $((elapsed%3600/60)) $((elapsed%60)))

    if [ $exit_code -eq 0 ]; then
        hpc_notify "✅ Finished: ${job_label}${cmd} (took $duration)" 3
    else
        hpc_notify "❌ Failed (exit $exit_code): ${job_label}${cmd} (after $duration)" 5
    fi

    return $exit_code
}
