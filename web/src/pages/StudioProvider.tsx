import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type {
  Catalog,
  Execution,
  Checkpoint,
  AuditEvent,
  Operation,
  OperationResult,
  Variables,
} from "../domain";
import { api } from "../service";
import { configureStudioCatalog } from "./StudioDemoData";
import {
  emptyState,
  hydrateState,
  selectedCheckpoint,
  selectedRun,
  studioReducer,
  type DemoAction,
  type DemoState,
} from "./StudioState";
type ProjectionData = {
  catalog: Catalog;
  executions: Execution[];
  checkpoints: Checkpoint[];
  events: AuditEvent[];
};
type ModalName = "guide" | "fork" | "rollback" | null;
interface StudioContextValue {
  state: DemoState;
  dispatch: (action: DemoAction) => void;
  drawer: boolean;
  setDrawer: (value: boolean) => void;
  modal: ModalName;
  setModal: (value: ModalName) => void;
  modalTarget: { runId: string; cpId: string } | null;
  notify: (message: string) => void;
  message: string;
  saved: boolean;
  capabilities: Record<string, boolean>;
  unavailable: Record<string, string>;
  ready: boolean;
  connection: string;
  catalog?: Catalog;
  executions: Execution[];
  revision: number;
  busy: boolean;
  refresh: (selectId?: string) => Promise<void>;
  operate: (id: string, action: string, payload?: Variables) => Promise<void>;
  start: (
    variantId: string,
    state: Variables,
    breakpoints: string[],
    nodeVariants: Record<string, string>,
  ) => Promise<void>;
}
const StudioContext = createContext<StudioContextValue | null>(null);
export function StudioProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState(emptyState),
    [drawer, setDrawer] = useState(false),
    [modal, changeModal] = useState<ModalName>(null),
    [modalTarget, setTarget] =
      useState<StudioContextValue["modalTarget"]>(null);
  const [message, notify] = useState(""),
    [data, setData] = useState<ProjectionData>(),
    [connection, setConnection] = useState("connecting"),
    [revision, setRevision] = useState(0),
    [busy, setBusy] = useState(false);
  const inFlight = useRef<Promise<void> | null>(null),
    mutation = useRef(false),
    mounted = useRef(true);
  const refresh = useCallback(async (selectId?: string) => {
    if (inFlight.current) {
      await inFlight.current;
      if (!selectId) return;
    }
    const task = (async () => {
      try {
        const next = await api<ProjectionData>("/console-state");
        if (!mounted.current) return;
        // Retain frozen definitions for executions whose project was removed.
        const variants = [...next.catalog.variants];
        next.executions.forEach((e) => {
          if (!variants.some((v) => v.id === e.variantId))
            variants.push(e.graph);
        });
        configureStudioCatalog(variants);
        const cps: Record<string, Checkpoint[]> = {};
        next.checkpoints.forEach((cp) => (cps[cp.executionId] ??= []).push(cp));
        setState((previous) => {
          const s = hydrateState(previous, next.executions, cps, next.events);
          return selectId ? studioReducer(s, { type: "run", id: selectId }) : s;
        });
        setData(next);
        setRevision((r) => r + 1);
        setConnection("connected");
      } catch (error) {
        if (mounted.current) {
          setConnection("offline");
          notify(`无法连接本机 WTB：${String(error)}`);
        }
      }
    })();
    inFlight.current = task;
    try {
      await task;
    } finally {
      if (inFlight.current === task) inFlight.current = null;
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    let stopped = false,
      socket: WebSocket | undefined,
      retry: number | undefined,
      debounce: number | undefined;
    const open = () => {
      if (stopped) return;
      socket = new WebSocket(
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`,
      );
      socket.onmessage = () => {
        clearTimeout(debounce);
        debounce = window.setTimeout(() => void refresh(), 100);
      };
      socket.onclose = () => {
        if (!stopped) retry = window.setTimeout(open, 2000);
      };
      socket.onerror = () => socket?.close();
    };
    open();
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      stopped = true;
      mounted.current = false;
      clearInterval(timer);
      clearTimeout(retry);
      clearTimeout(debounce);
      socket?.close();
    };
  }, [refresh]);
  useEffect(() => {
    document.body.dataset.theme = "cyber";
    document.body.dataset.view = state.view;
  }, [state.view]);
  useEffect(() => {
    if (message) {
      const timer = setTimeout(() => notify(""), 5000);
      return () => clearTimeout(timer);
    }
  }, [message]);
  const waitOperation = async (job: OperationResult) => {
    for (let i = 0; i < 600; i++) {
      const op = await api<Operation>(`/operations/${job.operationId}`);
      if (!["queued", "running"].includes(op.status)) {
        if (op.status !== "completed") throw new Error(op.error || "操作失败");
        return op.resultExecutionId;
      }
      await new Promise((r) => setTimeout(r, 100));
    }
    throw new Error("操作仍在后台执行，请查看执行状态后再操作。");
  };
  const mutate = async (fn: () => Promise<void>) => {
    if (mutation.current) throw new Error("请等待当前操作完成");
    mutation.current = true;
    setBusy(true);
    try {
      await fn();
    } catch (error) {
      notify(String(error));
      throw error;
    } finally {
      mutation.current = false;
      setBusy(false);
    }
  };
  const operate = async (id: string, action: string, payload: Variables = {}) =>
    mutate(async () => {
      if (!id || connection === "offline")
        throw new Error("请先选择在线的真实执行");
      const job = await api<OperationResult>(
          `/executions/${id}/${action}`,
          payload,
        ),
        child = await waitOperation(job);
      if (inFlight.current) await inFlight.current;
      await refresh(child || id);
      notify("操作已完成");
    });
  const start = async (
    variantId: string,
    values: Variables,
    breakpoints: string[],
    nodeVariants: Record<string, string>,
  ) =>
    mutate(async () => {
      const variant = data?.catalog.variants.find((v) => v.id === variantId);
      if (!variant) throw new Error("请选择已注册变体");
      const job = await api<OperationResult>(
        `/workflows/${encodeURIComponent(variant.projectId)}/execute`,
        { variantId, state: values, breakpoints, nodeVariants },
      );
      await refresh(job.executionId);
      notify("已启动真实执行");
    });
  const setModal = (value: ModalName) => {
    if (value === "fork" || value === "rollback") {
      const r = selectedRun(state),
        cp = selectedCheckpoint(state);
      if (
        !r.id ||
        !cp.restorable ||
        busy ||
        connection === "offline" ||
        ["queued", "running", "pausing", "stopping"].includes(r.status)
      ) {
        notify("请等待执行暂停或结束，并选择有效检查点");
        return;
      }
      setTarget({ runId: r.id, cpId: cp.id });
    } else setTarget(null);
    changeModal(value);
  };
  const dispatch = (action: DemoAction) => {
    const r = selectedRun(state);
    if (action.type === "reset") {
      void refresh();
      return;
    }
    if (action.type === "pause" || action.type === "step") {
      const command =
        action.type === "step"
          ? "resume"
          : r.status === "running"
            ? "pause"
            : "resume";
      if (!r.id || (command === "resume" && r.status !== "paused")) return;
      void operate(r.id, command).catch(() => {});
      return;
    }
    if (action.type === "restore") {
      void operate(
        action.runId,
        action.kind === "fork" ? "branches" : "rollback",
        { checkpointId: action.cpId },
      )
        .then(() => setModal(null))
        .catch(() => {});
      return;
    }
    setState((previous) => studioReducer(previous, action));
  };
  return (
    <StudioContext.Provider
      value={{
        state,
        dispatch,
        drawer,
        setDrawer,
        modal,
        setModal,
        modalTarget,
        notify,
        message,
        saved: connection === "connected",
        connection,
        capabilities: data?.catalog.capabilities || {},
        unavailable: data?.catalog.unavailable || {},
        ready: !!data,
        catalog: data?.catalog,
        executions: data?.executions || [],
        revision,
        busy,
        refresh,
        operate,
        start,
      }}
    >
      {children}
    </StudioContext.Provider>
  );
}
export function useStudio() {
  const value = useContext(StudioContext);
  if (!value) throw new Error("StudioProvider is required");
  return value;
}
