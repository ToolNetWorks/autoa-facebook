import { useId, type ReactNode } from 'react';

interface CheckboxProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  name?: string;
  value?: string;
}

export function Checkbox({
  checked,
  onChange,
  title,
  description,
  disabled,
  name,
  value,
}: CheckboxProps) {
  const id = useId();
  return (
    <label className="check" htmlFor={id}>
      <input
        id={id}
        type="checkbox"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="check-text">
        <b>{title}</b>
        {description ? <small>{description}</small> : null}
      </span>
    </label>
  );
}

interface RadioProps {
  checked: boolean;
  onChange: () => void;
  title: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  name: string;
  value: string;
}

export function Radio({ checked, onChange, title, description, disabled, name, value }: RadioProps) {
  const id = useId();
  return (
    <label className="choice" htmlFor={id}>
      <input
        id={id}
        type="radio"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={onChange}
      />
      <span className="check-text">
        <b>{title}</b>
        {description ? <small>{description}</small> : null}
      </span>
    </label>
  );
}

interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  id?: string;
}

export function Switch({ checked, onChange, label, description, disabled, id }: SwitchProps) {
  const generated = useId();
  const inputId = id ?? generated;
  return (
    <label className="switch" htmlFor={inputId}>
      <input
        id={inputId}
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="switch-track" aria-hidden="true" />
      <span className="check-text">
        <b>{label}</b>
        {description ? <small>{description}</small> : null}
      </span>
    </label>
  );
}
