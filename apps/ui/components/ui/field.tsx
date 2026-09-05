"use client"

import { Field as FieldPrimitive } from "@base-ui/react/field"

import { cn } from "@/lib/utils"

type ClassNameProp<State> = string | ((state: State) => string | undefined) | undefined

// Base UI's className prop can be a plain string or a callback that receives the
// component's state (e.g. touched/dirty/valid) -- cn() only understands strings, so a
// callback passed straight through would be silently dropped instead of evaluated.
// This preserves the callback (merging its result with the default classes at call
// time) or merges directly when className is already a string.
function withDefaultClassName<State>(
  defaultClassName: string,
  className: ClassNameProp<State>,
): string | ((state: State) => string) {
  if (typeof className === "function") {
    return (state: State) => cn(defaultClassName, className(state))
  }
  return cn(defaultClassName, className)
}

// Wraps Base UI's Field so plain <label>text</label> pairs (visually adjacent to their
// input but not programmatically associated -- a screen reader won't announce the label
// on focus) become a real <label htmlFor="..."> / <input id="..."> pair for free. Any
// Base UI-based control (this project's Input included) picks up the generated id
// automatically just by rendering inside <Field>, no extra wiring needed per field.
function Field({ className, ...props }: FieldPrimitive.Root.Props) {
  return <FieldPrimitive.Root data-slot="field" className={withDefaultClassName("", className)} {...props} />
}

function FieldLabel({ className, ...props }: FieldPrimitive.Label.Props) {
  return (
    <FieldPrimitive.Label
      data-slot="field-label"
      className={withDefaultClassName("text-xs font-medium text-foreground block mb-1.5", className)}
      {...props}
    />
  )
}

function FieldDescription({ className, ...props }: FieldPrimitive.Description.Props) {
  return (
    <FieldPrimitive.Description
      data-slot="field-description"
      className={withDefaultClassName("text-xs text-muted-foreground mt-1", className)}
      {...props}
    />
  )
}

function FieldError({ className, ...props }: FieldPrimitive.Error.Props) {
  return (
    <FieldPrimitive.Error
      data-slot="field-error"
      className={withDefaultClassName("text-xs text-destructive mt-1", className)}
      {...props}
    />
  )
}

export { Field, FieldLabel, FieldDescription, FieldError }
