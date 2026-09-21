import { useEffect, useRef, type ReactNode } from "react";
import { COMPONENTS, LABELS, NODESTATUS } from "./StudioDemoData";
import { CopyButton } from "./StudioContextRail";
import { StudioGuide } from "./StudioGuide";
import { StudioIcon } from "./StudioIcon";
import { useStudio } from "./StudioProvider";
import {
  nodeContext,
  selectedCheckpoint,
  selectedRun,
  selectedVariant,
} from "./StudioState";

function ContextRow({
  label,
  value,
  copy = false,
}: {
  label: string;
  value: string;
  copy?: boolean;
}) {
  return (
    <div className="row">
      <span>{label}</span>
      <div>
        <code>{value}</code>
        {copy && (
          <>
            {" "}
            <CopyButton value={value} label={`复制 ${label}`} />
          </>
        )}
      </div>
    </div>
  );
}
export function StudioDrawer() {
  const { state, dispatch, drawer, setDrawer, unavailable } = useStudio(),
    r = selectedRun(state),
    c = nodeContext(state),
    cp = selectedCheckpoint(state),
    v = selectedVariant(state),
    ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!drawer) return;
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const trap = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const items = [
        ...ref.current!.querySelectorAll<HTMLElement>(
          'button:not(:disabled),[tabindex="0"]',
        ),
      ];
      if (e.shiftKey && document.activeElement === items[0]) {
        e.preventDefault();
        items.at(-1)?.focus();
      } else if (!e.shiftKey && document.activeElement === items.at(-1)) {
        e.preventDefault();
        items[0]?.focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => {
      document.removeEventListener("keydown", trap);
      if (previous?.isConnected) previous.focus();
    };
  }, [drawer]);
  return (
    <>
      <div
        className={`backdrop ${drawer ? "open" : ""}`}
        id="drawer-backdrop"
        onClick={() => setDrawer(false)}
      />
      <aside
        ref={ref}
        className={`drawer ${drawer ? "open" : ""}`}
        id="drawer"
        aria-label="执行上下文"
        aria-modal="true"
        role="dialog"
        aria-hidden={!drawer}
        inert={!drawer}
      >
        <div className="drawer-top">
          <span className="upper">EXECUTION CONTEXT / LIVE</span>
          <button
            className="text-btn"
            aria-label="关闭执行上下文"
            onClick={() => setDrawer(false)}
          >
            <StudioIcon name="close" size={19} />
          </button>
        </div>
        <div className="upper muted">
          {v.name} / {r.id}
        </div>
        <h2>The world beneath {c.c}.</h2>
        <div className="drawer-node-tabs">
          {COMPONENTS.map((a) => (
            <button
              key={a.id}
              className={a.id === c.c ? "active" : ""}
              title={a.id}
              aria-label={`选择节点 ${a.id}`}
              onClick={() => dispatch({ type: "node", id: a.id })}
            >
              {a.id}
            </button>
          ))}
        </div>
        <ContextRow label="节点状态" value={NODESTATUS[c.ns]} />
        <ContextRow label="实现 / 配置" value={LABELS[String(c.cfg)]} />
        <ContextRow
          label="执行方式"
          value={r.mode === "ray" ? "Ray actor" : "Local worker thread"}
        />
        <ContextRow
          label="执行器"
          value={c.inherited ? "无当前执行器；继承检查点状态" : c.actor}
          copy={c.allocated}
        />
        <ContextRow label="宿主" value={c.allocated ? c.host : "未分配"} />
        <h3>01 / 路径：控制端与执行端分开</h3>
        <ContextRow label="本地根目录" value={c.workspace} copy />
        <ContextRow
          label="执行目录"
          value={c.allocated ? `${c.host}:${c.workdir}` : "待节点重新调度"}
          copy={c.allocated}
        />
        <h3>02 / 环境：实际服务进程</h3>
        <ContextRow
          label="Python"
          value={c.envPath || unavailable.venv || "未提供"}
          copy={!!c.envPath}
        />
        <ContextRow label="Python 版本" value={c.envSpec} />
        <ContextRow label="隔离环境" value={unavailable.venv || "由后端声明"} />
        <h3>03 / 所选检查点</h3>
        <ContextRow label="checkpoint_id" value={cp.id} copy />
        <ContextRow
          label="文件恢复范围"
          value={cp.tracked + " tracked artifacts"}
        />
        <ContextRow label="CAS（仅审计）" value={cp.cas} />
        <div className="notice">
          恢复范围 = 图状态 + 已声明跟踪的文件。Actor、venv
          和外部工具副作用不属于检查点恢复范围；当前未接入能力会由后端明确标记。
        </div>
        <h3>04 / 运行事件</h3>
        <div className="events">
          {[...r.events].reverse().map((e, i) => (
            <div className="event" key={`${e.at}-${i}`}>
              <span>{e.at}</span>
              <span>{e.text}</span>
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}
function Modal({
  open,
  id,
  className,
  children,
}: {
  open: boolean;
  id: string;
  className?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null),
    { setModal } = useStudio();
  useEffect(() => {
    if (open && !ref.current?.open) ref.current?.showModal();
    else if (!open && ref.current?.open) ref.current.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      id={id}
      className={className}
      onCancel={() => setModal(null)}
    >
      {children}
    </dialog>
  );
}
export function StudioActionDialog() {
  const { state, dispatch, modal, setModal, modalTarget, busy } = useStudio(),
    r =
      state.runs.find((r) => r.id === modalTarget?.runId) || selectedRun(state),
    cp =
      state.checkpoints[modalTarget?.cpId || ""] || selectedCheckpoint(state),
    fork = modal === "fork";
  return (
    <Modal id="action-dialog" open={modal === "fork" || modal === "rollback"}>
      <div className="modal-eyebrow">
        {fork ? "NON-DESTRUCTIVE FORK" : "CHECKPOINT RESTORE"}
      </div>
      <h2>{fork ? "让历史长出一条新分支。" : "回到检查点，不抹去历史。"}</h2>
      <p>
        {fork ? (
          <>
            从 <b>{cp.id}</b> 创建新的真实运行实例。当前 {r.id}{" "}
            不变，新运行从独立 workspace 继续。变体定义仍是{" "}
            {selectedVariant(state).name}。
          </>
        ) : (
          <>
            在 <b>{r.id}</b> 内，从 <b>{cp.id}</b>{" "}
            回退。之前的节点记录与检查点保留。
          </>
        )}
      </p>
      <div className="scope-line">
        <span>恢复图状态</span>
        <span>{cp.step === 0 ? "初始状态" : cp.node + " 完成后"}</span>
      </div>
      <div className="scope-line">
        <span>已跟踪文件</span>
        <span>{cp.tracked} 项 · checkpoint-linked CAS</span>
      </div>
      <div className="scope-line">
        <span>执行进程 / Ray Actor</span>
        <span>继续时重新分配</span>
      </div>
      <div className="scope-line">
        <span>venv / 外部工具副作用</span>
        <span>不保证恢复</span>
      </div>
      <p className="small">该操作将提交给本机 WTB 服务，并以服务端结果为准。</p>
      <div className="modal-actions">
        <button className="outline-btn" onClick={() => setModal(null)}>
          取消
        </button>
        <button
          className="accent-btn"
          disabled={busy || !cp.restorable}
          onClick={() => {
            dispatch({
              type: "restore",
              kind: fork ? "fork" : "rollback",
              cpId: cp.id,
              runId: r.id,
            });
          }}
        >
          <StudioIcon name={fork ? "fork" : "back"} />
          {fork ? " 创建分支" : " 确认回退"}
        </button>
      </div>
    </Modal>
  );
}
export function StudioOverlays() {
  const { modal, setModal, message } = useStudio();
  return (
    <>
      <div
        className={`toast ${message ? "visible" : ""}`}
        id="toast"
        role="status"
        aria-live="polite"
      >
        {message}
      </div>
      <StudioDrawer />
      <StudioActionDialog />
      <Modal id="guide-dialog" className="guide" open={modal === "guide"}>
        <StudioGuide onClose={() => setModal(null)} />
      </Modal>
    </>
  );
}
