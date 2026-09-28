/**
 * Radix-Dialog wrapper used as the single modal primitive across the app.
 *
 * Why this exists:
 *   - Replaces the ad-hoc `fixed inset-0 z-50 …` blob that PAT, speaker
 *     library, and other custom modals copy-pasted.
 *   - Radix Dialog gives us `role="dialog"` + `aria-modal="true"` + focus
 *     trap + focus return + ESC-to-close + click-outside semantics for
 *     free, so the Codex audit's a11y findings on the custom modals go
 *     away once callers swap in.
 *   - Callers stay control: they pass their own header / body / footer.
 *     We expose `<Dialog.Title>` + `<Dialog.Description>` re-exports so
 *     a11y wiring is opt-in rather than implicit.
 *
 * Usage:
 *   <Dialog open={open} onOpenChange={setOpen}>
 *     <Dialog.Content size="md" titleId="my-dialog-title">
 *       <Dialog.Header>
 *         <Dialog.Title id="my-dialog-title">Heading</Dialog.Title>
 *         <Dialog.Close />
 *       </Dialog.Header>
 *       <div className="px-5 py-4">…body…</div>
 *       <Dialog.Footer>…actions…</Dialog.Footer>
 *     </Dialog.Content>
 *   </Dialog>
 */
import { forwardRef, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import * as RadixDialog from '@radix-ui/react-dialog';
import { X } from 'lucide-react';

type Size = 'sm' | 'md' | 'lg' | 'xl';

const sizeClasses: Record<Size, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-xl',
};

interface DialogRootProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
  /** When true the modal won't close on outside-click / Esc. Used for
   *  destructive flows where an accidental dismiss would be costly. */
  modal?: boolean;
}

function Dialog({ open, onOpenChange, children, modal = true }: DialogRootProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange} modal={modal}>
      {children}
    </RadixDialog.Root>
  );
}

interface ContentProps extends ComponentPropsWithoutRef<typeof RadixDialog.Content> {
  size?: Size;
  /** Pass through to Radix so screen readers announce a description. */
  describedById?: string;
  /** Custom className appended to the default content shell. */
  contentClassName?: string;
}

const Content = forwardRef<HTMLDivElement, ContentProps>(function Content(
  { size = 'md', describedById, contentClassName = '', children, ...rest },
  ref,
) {
  return (
    <RadixDialog.Portal>
      <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/70 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0" />
      <RadixDialog.Content
        ref={ref}
        aria-describedby={describedById}
        className={`fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 ${sizeClasses[size]} rounded-2xl border border-zinc-800 bg-zinc-900 shadow-2xl shadow-black/60 outline-none focus:outline-none ${contentClassName}`}
        {...rest}
      >
        {children}
      </RadixDialog.Content>
    </RadixDialog.Portal>
  );
});

function Header({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`flex items-start justify-between gap-3 border-b border-zinc-800 px-5 py-4 ${className}`}>
      {children}
    </div>
  );
}

function Footer({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`flex items-center justify-end gap-2 border-t border-zinc-800 px-5 py-3 ${className}`}>
      {children}
    </div>
  );
}

const Title = forwardRef<
  HTMLHeadingElement,
  ComponentPropsWithoutRef<typeof RadixDialog.Title>
>(function Title({ className = '', ...rest }, ref) {
  return (
    <RadixDialog.Title
      ref={ref}
      className={`text-base font-semibold text-white ${className}`}
      {...rest}
    />
  );
});

const Description = forwardRef<
  HTMLParagraphElement,
  ComponentPropsWithoutRef<typeof RadixDialog.Description>
>(function Description({ className = '', ...rest }, ref) {
  return (
    <RadixDialog.Description
      ref={ref}
      className={`text-sm leading-6 text-zinc-300 ${className}`}
      {...rest}
    />
  );
});

interface CloseProps {
  label?: string;
  className?: string;
}

function Close({ label = 'Close', className = '' }: CloseProps) {
  return (
    <RadixDialog.Close asChild>
      <button
        type="button"
        aria-label={label}
        className={`rounded-md p-1 text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-200 ${className}`}
      >
        <X className="h-4 w-4" />
      </button>
    </RadixDialog.Close>
  );
}

Dialog.Content = Content;
Dialog.Header = Header;
Dialog.Footer = Footer;
Dialog.Title = Title;
Dialog.Description = Description;
Dialog.Close = Close;

export { Dialog };
export default Dialog;
