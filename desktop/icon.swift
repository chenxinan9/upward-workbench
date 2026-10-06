import AppKit
let size=NSSize(width:1024,height:1024)
let image=NSImage(size:size)
image.lockFocus()
NSColor(red:0.20,green:0.34,blue:1.0,alpha:1).setFill()
NSBezierPath(roundedRect:NSRect(x:64,y:64,width:896,height:896),xRadius:210,yRadius:210).fill()
NSColor.white.setStroke()
let arrow=NSBezierPath();arrow.lineWidth=80;arrow.lineCapStyle = .round;arrow.lineJoinStyle = .round
arrow.move(to:NSPoint(x:295,y:295));arrow.line(to:NSPoint(x:720,y:720));arrow.move(to:NSPoint(x:360,y:720));arrow.line(to:NSPoint(x:720,y:720));arrow.line(to:NSPoint(x:720,y:360));arrow.stroke()
image.unlockFocus()
let bitmap=NSBitmapImageRep(data:image.tiffRepresentation!)!
try bitmap.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:CommandLine.arguments[1]))
