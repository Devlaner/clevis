"use client"

import { Toast as ToastPrimitive } from "@base-ui/react/toast"
import { X } from "@phosphor-icons/react"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"

// A manager created outside React so `toast.success(...)`/`toast.error(...)` can be
// called from anywhere (mutation onSuccess/onError callbacks, plain functions) without
// needing to be inside a component that calls useToastManager(). <Toaster/> (mounted
// once in app/layout.tsx) wires this same instance into ToastPrimitive.Provider.
const toastManager = ToastPrimitive.createToastManager()

export const toast = {
  success: (description: string, title?: string) =>
    toastManager.add({ type: "success", title, description }),
  error: (description: string, title?: string) =>
    toastManager.add({ type: "error", title, description }),
  info: (description: string, title?: string) =>
    toastManager.add({ type: "info", title, description }),
}

const TYPE_ACCENT: Record<string, string> = {
  success: "border-l-emerald-500/70",
  error: "border-l-destructive/70",
  info: "border-l-primary/70",
}

function ToastList() {
  const { toasts } = ToastPrimitive.useToastManager()

  return toasts.map((t) => (
    <ToastPrimitive.Root
      key={t.id}
      toast={t}
      className={cn(
        "absolute inset-x-0 bottom-0 rounded-md border border-l-2 border-border bg-popover bg-clip-padding p-3 pr-8 text-popover-foreground shadow-lg transition-[transform,opacity] duration-200 ease-(--ease-out) data-ending-style:opacity-0 data-starting-style:translate-y-[150%] data-starting-style:opacity-0",
        t.type && TYPE_ACCENT[t.type]
      )}
      style={{
        zIndex: "calc(1000 - var(--toast-index))",
        transform: "translateY(var(--toast-offset-y)) scale(calc(max(0, 1 - (var(--toast-index) * 0.1))))",
      }}
    >
      <ToastPrimitive.Content className="flex flex-col gap-0.5">
        {t.title && (
          <ToastPrimitive.Title className="text-sm font-medium text-foreground">
            {t.title}
          </ToastPrimitive.Title>
        )}
        <ToastPrimitive.Description className="text-sm text-muted-foreground">
          {t.description}
        </ToastPrimitive.Description>
      </ToastPrimitive.Content>
      <ToastPrimitive.Close
        render={<Button variant="ghost" size="icon-xs" className="absolute top-2 right-2" />}
      >
        <X />
        <span className="sr-only">Dismiss</span>
      </ToastPrimitive.Close>
    </ToastPrimitive.Root>
  ))
}

export function Toaster() {
  return (
    <ToastPrimitive.Provider toastManager={toastManager}>
      <ToastPrimitive.Portal>
        <ToastPrimitive.Viewport className="fixed bottom-4 right-4 z-50 flex w-full max-w-sm flex-col-reverse gap-2">
          <ToastList />
        </ToastPrimitive.Viewport>
      </ToastPrimitive.Portal>
    </ToastPrimitive.Provider>
  )
}
