import { Construction } from 'lucide-react';

interface EmptyPanelProps {
  title: string;
  description?: string;
}

/**
 * Placeholder shell used for Settings groups that are part of the new
 * 4-tier IA (My preferences / Recording defaults / Workspace settings /
 * Admin & appliance) but whose individual panels haven't been built yet.
 *
 * Intentionally inert — no controls, no state, no save button. Reading
 * Aaron's UX-A scope: "New panels get placeholder shells… don't
 * implement their guts."
 */
export default function EmptyPanel({ title, description }: EmptyPanelProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <Construction className="w-12 h-12 text-zinc-700 mb-4" />
      <h3 className="text-lg font-medium text-zinc-200 mb-2">{title}</h3>
      <p className="text-sm text-zinc-500 max-w-md">
        {description || 'Coming in a future release'}
      </p>
    </div>
  );
}
