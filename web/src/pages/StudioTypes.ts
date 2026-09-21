export type Projection = "loom" | "lineage" | "section" | "atlas";

export type StudioPageProps = {
  onProjectionChange: (projection: Projection) => void;
};

export const projections: Array<{
  id: Projection;
  number: string;
  label: string;
  english: string;
}> = [
  { id: "loom", number: "01", label: "路径织谱", english: "PATH LOOM" },
  { id: "lineage", number: "02", label: "分支地形", english: "BRANCH ATLAS" },
  {
    id: "section",
    number: "03",
    label: "执行剖面",
    english: "EXECUTION SECTION",
  },
  { id: "atlas", number: "04", label: "变体图谱", english: "VARIANT ATLAS" },
];
