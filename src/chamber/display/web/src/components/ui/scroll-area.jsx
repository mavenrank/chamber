import React from "react";
import * as ScrollAreaPrimitive from "@radix-ui/react-scroll-area";

// This follows the shadcn/ui ScrollArea composition. The visual treatment stays
// in the Chamber stylesheet so the companion keeps its monochrome identity.
export const ScrollArea = React.forwardRef(function ScrollArea(
  { children, className = "", onScroll, ...props },
  ref,
) {
  return (
    <ScrollAreaPrimitive.Root type="always" className={`scroll-area ${className}`} {...props}>
      <ScrollAreaPrimitive.Viewport ref={ref} className="scroll-area-viewport" onScroll={onScroll}>
        {children}
      </ScrollAreaPrimitive.Viewport>
      <ScrollAreaPrimitive.Scrollbar orientation="vertical" className="scroll-area-scrollbar">
        <ScrollAreaPrimitive.Thumb className="scroll-area-thumb" />
      </ScrollAreaPrimitive.Scrollbar>
      <ScrollAreaPrimitive.Corner />
    </ScrollAreaPrimitive.Root>
  );
});
