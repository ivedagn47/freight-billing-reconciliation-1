# Sourced by the flow's scripts: run inputs may be given relative to the repository root. Script nodes run in
# the run's artefact directory, so a relative path would otherwise silently point inside the run.
FREIGHT_REPO="$(cd "$FLOWSTATE_VAR__flow_dir/../../.." && pwd)"
repo_path() {
  case "$1" in
    "") printf '' ;;
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$FREIGHT_REPO" "$1" ;;
  esac
}
