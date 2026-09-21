import type { ReactNode, SVGProps } from "react";
export function StudioSvg({
  height = 453,
  label,
  children,
}: {
  height?: number;
  label: string;
  children: ReactNode;
}) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox={`0 0 1360 ${height}`}
      role="img"
      aria-label={label}
    >
      <defs>
        <pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse">
          <circle cx="1" cy="1" r=".65" fill="var(--line)" opacity=".55" />
        </pattern>
        <marker
          id="arrowhead"
          viewBox="0 0 8 8"
          refX="6"
          refY="4"
          markerWidth="5"
          markerHeight="5"
          orient="auto-start-reverse"
        >
          <path
            d="M1 1 6 4 1 7"
            fill="none"
            stroke="var(--muted)"
            strokeWidth="1"
          />
        </marker>
      </defs>
      {children}
    </svg>
  );
}
export function Text({
  x,
  y,
  children,
  size = 11,
  color = "var(--ink)",
  mono = false,
  ...props
}: SVGProps<SVGTextElement> & {
  size?: number;
  color?: string;
  mono?: boolean;
}) {
  return (
    <text
      x={x}
      y={y}
      fontSize={size}
      fill={color}
      style={mono ? { fontFamily: "var(--mono)" } : undefined}
      {...props}
    >
      {children}
    </text>
  );
}
export function Mono(props: Parameters<typeof Text>[0]) {
  return <Text size={10} color="var(--muted)" {...props} mono />;
}
export function Hit({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <g
      className="hit"
      tabIndex={0}
      role="button"
      aria-label={label}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          onClick();
        }
      }}
    >
      {children}
      <title>{label}</title>
    </g>
  );
}
export function Pill({
  x,
  y,
  text,
  color = "var(--accent)",
}: {
  x: number;
  y: number;
  text: string;
  color?: string;
}) {
  return (
    <>
      <rect
        x={x}
        y={y - 12}
        width={text.length * 6.2 + 18}
        height={20}
        rx={4}
        fill="var(--surface)"
      />
      <Text x={x + 9} y={y + 2} size={9} color={color}>
        {text}
      </Text>
    </>
  );
}
