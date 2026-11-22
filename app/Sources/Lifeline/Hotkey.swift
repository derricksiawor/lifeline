import Carbon
import LifelineKit

private let hotkeySignature: OSType = 0x4C46_4C4E
private let hotkeyNumber: UInt32 = 1

@MainActor
final class Hotkey: ObservableObject {
    @Published private(set) var state: ShortcutState = .waiting
    private let pressed: () -> Void
    private var reference: EventHotKeyRef?
    private var handler: EventHandlerRef?

    init(pressed: @escaping () -> Void) {
        self.pressed = pressed
    }

    func register() {
        guard reference == nil else { return }
        if handler == nil {
            var kind = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
            let context = Unmanaged.passUnretained(self).toOpaque()
            let installed = InstallEventHandler(GetApplicationEventTarget(), { _, event, context in
                guard let event, let context else {
                    return OSStatus(eventNotHandledErr)
                }
                var found = EventHotKeyID()
                let read = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &found
                )
                guard read == noErr, found.signature == hotkeySignature, found.id == hotkeyNumber else {
                    return OSStatus(eventNotHandledErr)
                }
                let owner = Unmanaged<Hotkey>.fromOpaque(context).takeUnretainedValue()
                Task { @MainActor in
                    owner.pressed()
                }
                return noErr
            }, 1, &kind, context, &handler)
            guard installed == noErr else {
                state = .refused
                return
            }
        }
        var made: EventHotKeyRef?
        let status = RegisterEventHotKey(
            Shortcut.keyCode,
            UInt32(controlKey | optionKey),
            EventHotKeyID(signature: hotkeySignature, id: hotkeyNumber),
            GetApplicationEventTarget(),
            0,
            &made
        )
        state = Shortcut.state(forStatus: status)
        if status == noErr {
            reference = made
        }
    }

    func unregister() {
        if let reference {
            UnregisterEventHotKey(reference)
            self.reference = nil
        }
        if let handler {
            RemoveEventHandler(handler)
            self.handler = nil
        }
    }
}
