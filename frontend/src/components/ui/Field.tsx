import { useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from 'react';

interface FieldShellProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  id: string;
  children: ReactNode;
}

function FieldShell({ label, hint, error, required, id, children }: FieldShellProps) {
  return (
    <div className="field">
      {label ? (
        <label htmlFor={id}>
          {label}
          {required ? <span className="req" aria-hidden="true"> *</span> : null}
        </label>
      ) : null}
      {children}
      {error ? (
        <span className="field-error" role="alert">
          {error}
        </span>
      ) : hint ? (
        <span className="hint">{hint}</span>
      ) : null}
    </div>
  );
}

type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'id'> & {
  label?: ReactNode;
  hint?: ReactNode;
  error?: string | null;
};

export function Input({ label, hint, error, required, className = '', ...rest }: InputProps) {
  const generated = useId();
  const id = rest.name ? `field-${rest.name}` : generated;
  return (
    <FieldShell label={label} hint={hint} error={error} required={required} id={id}>
      <input
        id={id}
        className={`input ${error ? 'invalid' : ''} ${className}`.trim()}
        required={required}
        aria-invalid={error ? true : undefined}
        {...rest}
      />
    </FieldShell>
  );
}

type TextareaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'id'> & {
  label?: ReactNode;
  hint?: ReactNode;
  error?: string | null;
};

export function Textarea({ label, hint, error, required, className = '', ...rest }: TextareaProps) {
  const generated = useId();
  const id = rest.name ? `field-${rest.name}` : generated;
  return (
    <FieldShell label={label} hint={hint} error={error} required={required} id={id}>
      <textarea
        id={id}
        className={`textarea ${error ? 'invalid' : ''} ${className}`.trim()}
        required={required}
        aria-invalid={error ? true : undefined}
        {...rest}
      />
    </FieldShell>
  );
}

type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, 'id'> & {
  label?: ReactNode;
  hint?: ReactNode;
  error?: string | null;
};

export function Select({ label, hint, error, required, className = '', children, ...rest }: SelectProps) {
  const generated = useId();
  const id = rest.name ? `field-${rest.name}` : generated;
  return (
    <FieldShell label={label} hint={hint} error={error} required={required} id={id}>
      <select
        id={id}
        className={`select ${error ? 'invalid' : ''} ${className}`.trim()}
        required={required}
        aria-invalid={error ? true : undefined}
        {...rest}
      >
        {children}
      </select>
    </FieldShell>
  );
}
