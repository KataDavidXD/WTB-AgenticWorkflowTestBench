import { useEffect, type ReactNode } from "react";
import { StudioMasthead } from "./StudioMasthead";
import { StudioProjectionNav } from "./StudioProjectionNav";
import { StudioHero } from "./StudioHero";
import { StudioExecutionDock } from "./StudioExecutionDock";
import { StudioOverlays } from "./StudioOverlays";
import { useStudio } from "./StudioProvider";
import { selectedRun } from "./StudioState";
import { projections } from "./StudioTypes";
export function StudioPageFrame({ children }: { children: ReactNode }) {
  const {
    state,
    dispatch,
    saved,
    connection,
    drawer,
    modal,
    setModal,
    setDrawer,
    notify,
  } = useStudio();
  useEffect(() => {
    const keydown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setDrawer(false);
        return;
      }
      const target = e.target as HTMLElement;
      if (
        ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName) ||
        modal ||
        drawer ||
        document.querySelector(".modal-backdrop")
      )
        return;
      if (
        target.closest('[role="button"]') ||
        (target.tagName === "BUTTON" && ["Enter", " "].includes(e.key))
      )
        return;
      if (["1", "2", "3", "4"].includes(e.key))
        dispatch({ type: "view", view: projections[Number(e.key) - 1].id });
      else if (e.key === " ") {
        e.preventDefault();
        dispatch({ type: "pause" });
      } else if (e.key.toLowerCase() === "f") setModal("fork");
      else if (e.key.toLowerCase() === "r") {
        if (["running", "pausing", "idle"].includes(selectedRun(state).status))
          notify("先暂停到节点边界，再执行回退。");
        else setModal("rollback");
      } else if (e.key === "?") setModal("guide");
    };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, [state, dispatch, drawer, modal, setDrawer, setModal, notify]);
  return (
    <>
      <div className="shell">
        <StudioMasthead />
        <StudioProjectionNav />
        <StudioHero />
        {children}
        <StudioExecutionDock />
        <footer className="bottom-line">
          <span>
            WTB / STRUCTURAL STUDIO{" "}
            <span id="persistence-label">
              {saved
                ? "· LIVE WTB STATE"
                : connection === "offline"
                  ? "· OFFLINE"
                  : "· CONNECTING"}
            </span>
          </span>
          <span>
            1–4 切换视图 &nbsp;·&nbsp; Space 暂停 / 继续 &nbsp;·&nbsp; F Fork
            &nbsp;·&nbsp; R 回退 &nbsp;·&nbsp; ? 结构说明
          </span>
        </footer>
      </div>
      <StudioOverlays />
    </>
  );
}
