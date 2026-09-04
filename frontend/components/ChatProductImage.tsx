import React, { useState } from 'react';
import { Package } from 'lucide-react';

const imageSource = (value?: string) => {
  const source = typeof value === 'string' ? value.trim() : '';
  if (source.startsWith('//')) return `https:${source}`;
  return /^(https?:\/\/|\/static\/)/i.test(source) ? source : '';
};

export const ChatProductImage: React.FC<{ conversationImage?: string; productImage?: string }> = ({ conversationImage, productImage }) => {
  const [failed, setFailed] = useState<Set<string>>(new Set());
  const src = [...new Set([imageSource(conversationImage), imageSource(productImage)])]
    .find((value) => value && !failed.has(value));
  return (
    <div className="flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface-strong)] text-[var(--text-soft)] sm:h-16 sm:w-16">
      {src ? (
        <img key={src} src={src} alt="商品图片" referrerPolicy="no-referrer"
          className="h-full w-full object-cover"
          onError={() => setFailed((current) => new Set(current).add(src))} />
      ) : <Package role="img" aria-label="暂无商品图片" className="h-6 w-6" strokeWidth={1.5} />}
    </div>
  );
};
