import { cn } from "@/lib/utils";

interface CardProps {
  children: React.ReactNode;
  className?: string;
  title?: string;
  icon?: React.ReactNode;
}

export function Card({ children, className, title, icon }: CardProps) {
  return (
    <div className={cn("bg-card border border-card-border rounded-lg p-4", className)}>
      {(title || icon) && (
        <div className="flex items-center gap-2 mb-3 text-sm font-medium text-muted uppercase tracking-wider">
          {icon}
          {title}
        </div>
      )}
      {children}
    </div>
  );
}
