import React from 'react';
import { X } from 'lucide-react';

interface TagChipProps {
  tag: string;
  onRemove?: () => void;
  onClick?: () => void;
  selected?: boolean;
  size?: 'sm' | 'md';
  variant?: 'default' | 'filter';
  title?: string;
}

export const TagChip: React.FC<TagChipProps> = ({
  tag,
  onRemove,
  onClick,
  selected = false,
  size = 'sm',
  variant = 'default',
  title,
}) => {
  const sizing =
    size === 'sm'
      ? 'text-xs px-2 py-0.5'
      : 'text-sm px-2.5 py-1';

  const palette = (() => {
    if (variant === 'filter') {
      return selected
        ? 'bg-purple-600 text-white border-purple-500 hover:bg-purple-700'
        : 'bg-gray-800 text-gray-300 border-gray-700 hover:bg-gray-700';
    }
    return 'bg-purple-500/20 text-purple-300 border-purple-500/30';
  })();

  const className = `inline-flex items-center gap-1 rounded-full border ${palette} ${sizing} font-medium transition-colors max-w-[180px]`;
  const resolvedTitle = title || tag;
  const inner = (
    <>
      <span className="truncate">{tag}</span>
      {onRemove && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          className="ml-0.5 -mr-0.5 rounded-full p-0.5 hover:bg-black/20 focus:outline-none"
          aria-label={`Remove tag ${tag}`}
        >
          <X className="w-3 h-3" />
        </button>
      )}
    </>
  );

  // Render as a button when interactive, span otherwise — keeps a11y +
  // semantics clean without dynamic JSX-element typing tricks.
  return onClick ? (
    <button type="button" onClick={onClick} title={resolvedTitle} className={className}>
      {inner}
    </button>
  ) : (
    <span title={resolvedTitle} className={className}>
      {inner}
    </span>
  );
};

export default TagChip;
