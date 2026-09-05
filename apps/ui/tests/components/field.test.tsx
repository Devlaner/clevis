import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Field, FieldLabel, FieldDescription, FieldError } from "@/components/ui/field";

afterEach(() => {
  cleanup();
});

// Base UI's className prop accepts either a plain string or a callback that receives
// the component's state (e.g. invalid/touched/dirty) -- these wrappers must merge
// default classes with both forms rather than only handling the string case.

describe("Field", () => {
  it("merges a string className with the wrapper's own classes", () => {
    render(
      <Field data-testid="field" className="custom-field">
        <FieldLabel>Email</FieldLabel>
      </Field>,
    );
    expect(screen.getByTestId("field")).toHaveClass("custom-field");
  });

  it("evaluates a callback className with the field's state and merges the result", () => {
    render(
      <Field data-testid="field" invalid className={(state) => (!state.valid ? "is-invalid" : "is-valid")}>
        <FieldLabel>Email</FieldLabel>
      </Field>,
    );
    const field = screen.getByTestId("field");
    expect(field).toHaveClass("is-invalid");
    expect(field).not.toHaveClass("is-valid");
  });
});

describe("FieldLabel", () => {
  it("merges a string className with the default label classes", () => {
    render(
      <Field>
        <FieldLabel data-testid="label" className="custom-label">
          Email
        </FieldLabel>
      </Field>,
    );
    const label = screen.getByTestId("label");
    expect(label).toHaveClass("custom-label");
    expect(label).toHaveClass("text-xs");
  });

  it("evaluates a callback className with the field's state and merges the result", () => {
    render(
      <Field invalid>
        <FieldLabel data-testid="label" className={(state) => (!state.valid ? "label-invalid" : undefined)}>
          Email
        </FieldLabel>
      </Field>,
    );
    const label = screen.getByTestId("label");
    expect(label).toHaveClass("label-invalid");
    expect(label).toHaveClass("text-xs");
  });
});

describe("FieldDescription", () => {
  it("merges a string className with the default description classes", () => {
    render(
      <Field>
        <FieldDescription data-testid="description" className="custom-description">
          Helper text
        </FieldDescription>
      </Field>,
    );
    const description = screen.getByTestId("description");
    expect(description).toHaveClass("custom-description");
    expect(description).toHaveClass("text-xs");
  });

  it("evaluates a callback className with the field's state and merges the result", () => {
    render(
      <Field invalid>
        <FieldDescription
          data-testid="description"
          className={(state) => (!state.valid ? "description-invalid" : undefined)}
        >
          Helper text
        </FieldDescription>
      </Field>,
    );
    const description = screen.getByTestId("description");
    expect(description).toHaveClass("description-invalid");
    expect(description).toHaveClass("text-xs");
  });
});

describe("FieldError", () => {
  it("merges a string className with the default error classes", () => {
    render(
      <Field invalid>
        <FieldError data-testid="error" match className="custom-error">
          Required
        </FieldError>
      </Field>,
    );
    const error = screen.getByTestId("error");
    expect(error).toHaveClass("custom-error");
    expect(error).toHaveClass("text-destructive");
  });

  it("evaluates a callback className with the field's state and merges the result", () => {
    render(
      <Field invalid>
        <FieldError data-testid="error" match className={(state) => (!state.valid ? "error-invalid" : undefined)}>
          Required
        </FieldError>
      </Field>,
    );
    const error = screen.getByTestId("error");
    expect(error).toHaveClass("error-invalid");
    expect(error).toHaveClass("text-destructive");
  });
});
