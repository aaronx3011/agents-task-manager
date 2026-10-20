import { Component, useState } from "react";
import type { ReactNode } from "react";
import { AlertCircle } from "lucide-react";

function RecoveryDialog({ close }: { close: () => void }) {
  // A plain overlay: native showModal() throws when a concurrent commit
  // detaches the element, and that error would loop through this boundary.
  const [visible] = useState(true);
  if (!visible) return null;
  return (
    <div
      className="recovery-layer"
      role="alertdialog"
      aria-labelledby="recovery-title"
      aria-describedby="recovery-description"
    >
      <div className="task-modal recovery-modal">
        <div className="recovery-content">
          <AlertCircle size={25} />
          <h2 id="recovery-title">Task details couldn’t load</h2>
          <p id="recovery-description">
            The view may have changed during an update, or the connection was
            interrupted. Reload to download the current version.
          </p>
          <div>
            <button className="button secondary" onClick={close}>
              Return to workspace
            </button>
            <button className="button" onClick={() => window.location.reload()}>
              Reload workspace
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export class ModalBoundary extends Component<
  { children: ReactNode; close: () => void },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <RecoveryDialog close={this.props.close} />
    ) : (
      this.props.children
    );
  }
}
