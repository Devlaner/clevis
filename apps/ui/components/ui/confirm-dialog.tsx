"use client"

import { AlertDialog as AlertDialogPrimitive } from "@base-ui/react/alert-dialog"

import { Button } from "@/components/ui/button"

interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel?: string
  cancelLabel?: string
  onConfirm: () => void
  /** Disables both buttons and shows a busy confirm label while a mutation is in flight.
   * The caller is responsible for calling onOpenChange(false) once it settles. */
  pending?: boolean
  variant?: "destructive" | "default"
}

// A real, accessible (focus-trapped, Escape-to-cancel) confirmation modal -- replaces the
// bespoke "click again within N seconds to confirm" pattern that used to be duplicated
// inline in cache-panel.tsx and settings/page.tsx.
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  onConfirm,
  pending = false,
  variant = "destructive",
}: ConfirmDialogProps) {
  return (
    <AlertDialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <AlertDialogPrimitive.Portal>
        <AlertDialogPrimitive.Backdrop className="fixed inset-0 z-50 bg-black/40 transition-opacity duration-150 ease-(--ease-out) data-ending-style:opacity-0 data-starting-style:opacity-0" />
        <AlertDialogPrimitive.Popup className="fixed top-1/2 left-1/2 z-50 w-[calc(100%-2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-popover bg-clip-padding p-5 text-popover-foreground shadow-lg transition-[transform,opacity] duration-150 ease-(--ease-out) data-ending-style:scale-95 data-ending-style:opacity-0 data-starting-style:scale-95 data-starting-style:opacity-0">
          <AlertDialogPrimitive.Title className="font-heading text-base font-medium text-foreground">
            {title}
          </AlertDialogPrimitive.Title>
          <AlertDialogPrimitive.Description className="mt-1.5 text-sm text-muted-foreground">
            {description}
          </AlertDialogPrimitive.Description>
          <div className="mt-4 flex justify-end gap-2">
            <AlertDialogPrimitive.Close
              render={<Button variant="outline" size="sm" disabled={pending} />}
            >
              {cancelLabel}
            </AlertDialogPrimitive.Close>
            <Button variant={variant} size="sm" disabled={pending} onClick={onConfirm}>
              {pending ? "Working…" : confirmLabel}
            </Button>
          </div>
        </AlertDialogPrimitive.Popup>
      </AlertDialogPrimitive.Portal>
    </AlertDialogPrimitive.Root>
  )
}
