"use client"

import { Field as FieldPrimitive } from "@base-ui/react/field"

import { cn } from "@/lib/utils"

// Wraps Base UI's Field so plain <label>text</label> pairs (visually adjacent to their
// input but not programmatically associated -- a screen reader won't announce the label
// on focus) become a real <label htmlFor="..."> / <input id="..."> pair for free. Any
// Base UI-based control (this project's Input included) picks up the generated id
// automatically just by rendering inside <Field>, no extra wiring needed per field.
function Field({ className, ...props }: FieldPrimitive.Root.Props) {
  return <FieldPrimitive.Root data-slot="field" className={cn(className)} {...props} />
}

function FieldLabel({ className, ...props }: FieldPrimitive.Label.Props) {
  return (
    <FieldPrimitive.Label
      data-slot="field-label"
      className={cn("text-xs font-medium text-foreground block mb-1.5", className)}
      {...props}
    />
  )
}

function FieldDescription({ className, ...props }: FieldPrimitive.Description.Props) {
  return (
    <FieldPrimitive.Description
      data-slot="field-description"
      className={cn("text-xs text-muted-foreground mt-1", className)}
      {...props}
    />
  )
}

function FieldError({ className, ...props }: FieldPrimitive.Error.Props) {
  return (
    <FieldPrimitive.Error
      data-slot="field-error"
      className={cn("text-xs text-destructive mt-1", className)}
      {...props}
    />
  )
}

export { Field, FieldLabel, FieldDescription, FieldError }
