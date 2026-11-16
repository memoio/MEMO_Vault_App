import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';

type Option = { value: string; label: string };
type Props = {
  label: string;
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  disabled?: boolean;
  icon?: string;
  prefix?: string;
  placement?: 'above' | 'below';
};

export default function ChatSelect({ label, value, options, onChange, disabled = false, icon, prefix, placement = 'above' }: Props) {
  const id = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const selected = options.findIndex(option => option.value === value);

  useEffect(() => {
    if (!open) return;
    function outside(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (open) document.getElementById(`${id}-option-${active}`)?.scrollIntoView({ block: 'nearest' });
  }, [active, id, open]);

  function choose(index: number) {
    const option = options[index];
    if (!option) return;
    onChange(option.value);
    setOpen(false);
    buttonRef.current?.focus();
  }

  function keyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      setOpen(false);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!options.length) return;
      if (!open) {
        setActive(selected >= 0 ? selected : 0);
        setOpen(true);
      } else {
        setActive(current => (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length);
      }
    } else if (open && (event.key === 'Home' || event.key === 'End')) {
      event.preventDefault();
      setActive(event.key === 'Home' ? 0 : options.length - 1);
    } else if (open && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      choose(active);
    } else if (event.key === 'Tab') {
      setOpen(false);
    }
  }

  return <div className={`chat-select chat-select-${placement} ${prefix ? 'chat-select-project' : ''}`} ref={rootRef}>
    <button ref={buttonRef} type="button" className="chat-select-trigger" role="combobox"
      aria-label={label} aria-expanded={open} aria-controls={`${id}-listbox`} aria-haspopup="listbox"
      aria-activedescendant={open && options[active] ? `${id}-option-${active}` : undefined}
      disabled={disabled || !options.length} onKeyDown={keyDown}
      onClick={() => { setActive(selected >= 0 ? selected : 0); setOpen(!open); }}>
      {icon && <span className="chat-select-icon" aria-hidden="true">{icon}</span>}
      {prefix && <span className="chat-select-prefix">{prefix}</span>}
      <span className="chat-select-value">{options[selected]?.label ?? (options.length ? '请选择' : '暂无选项')}</span>
      <svg className="chat-select-arrow" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 6 4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/></svg>
    </button>
    {open && <div className="chat-select-menu" id={`${id}-listbox`} role="listbox" aria-label={label}>
      {options.map((option, index) => <div id={`${id}-option-${index}`} role="option" aria-selected={option.value === value}
        className={`chat-select-option ${index === active ? 'is-active' : ''} ${option.value === value ? 'is-selected' : ''}`}
        key={option.value} onPointerMove={() => setActive(index)} onClick={() => choose(index)}>
        <span className="chat-select-option-name">{option.label}</span>
        {option.value === value && <svg className="chat-select-check" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m3.5 8.2 3 3 6-6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/></svg>}
      </div>)}
    </div>}
  </div>;
}
