defmodule HlPeers.GossipConfig.SaveResult do
  @moduledoc """
  What `HlPeers.GossipConfig.save/3` answers.

  * `:written` — bytes actually reached the filesystem;
  * `:changed` — the rendered text differs from what was on disk. `save` twice
    with no intervening change gives `written: false, changed: false`;
  * `:diff` — the unified diff, always populated under `dry_run: true`, empty
    when nothing changed;
  * `:backup_path` — set only when `backup: true` and the target existed;
  * `:resolved_path` — where the bytes actually landed. It differs from `:path`
    only when `:path` was a symlink, which `save/3` **follows** rather than
    replacing (CLIENT_SPEC §6.6);
  * `:warnings` — `HlPeers.Warning` structs from the `validate/1` that ran
    first. Never swallow them.
  """

  @type t :: %__MODULE__{
          written: boolean(),
          changed: boolean(),
          path: String.t(),
          diff: String.t(),
          backup_path: String.t() | nil,
          resolved_path: String.t() | nil,
          warnings: [HlPeers.Warning.t()]
        }

  defstruct written: false,
            changed: false,
            path: nil,
            diff: "",
            backup_path: nil,
            resolved_path: nil,
            warnings: []
end
