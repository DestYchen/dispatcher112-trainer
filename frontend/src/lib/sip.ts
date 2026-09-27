import { create } from "zustand";
import {
  Invitation,
  Inviter,
  Registerer,
  RegistererState,
  Session,
  SessionState,
  UserAgent,
  Web,
} from "sip.js";
import { api } from "../api/client";

interface PhoneState {
  diagnostic: boolean;
  connection: "OFF" | "CONNECTING" | "READY" | "ERROR";
  call: "IDLE" | "RINGING" | "DIALING" | "CONNECTED";
  error: string | null;
}
export const useSip = create<PhoneState>(() => ({
  diagnostic: false,
  connection: "OFF",
  call: "IDLE",
  error: null,
}));
let agent: UserAgent | null = null;
let registerer: Registerer | null = null;
let session: Session | null = null;
let remote: HTMLAudioElement | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let generation = 0;

function fail(error: unknown) {
  useSip.setState({
    error:
      error instanceof Error ? error.message : "Голосовая связь недоступна.",
  });
}
function attach(next: Session) {
  session = next;
  next.stateChange.addListener((state) => {
    if (session !== next) return;
    if (state === SessionState.Established) {
      const handler =
        next.sessionDescriptionHandler as Web.SessionDescriptionHandler;
      const stream = new MediaStream();
      for (const receiver of handler.peerConnection?.getReceivers() ?? []) {
        if ("jitterBufferTarget" in receiver) {
          (
            receiver as RTCRtpReceiver & { jitterBufferTarget: number }
          ).jitterBufferTarget = 20;
        }
        if (receiver.track) stream.addTrack(receiver.track);
      }
      if (remote) {
        remote.srcObject = stream;
        void remote.play().catch(fail);
      }
      useSip.setState({ call: "CONNECTED", error: null });
    } else if (state === SessionState.Terminated) {
      session = null;
      if (remote) remote.srcObject = null;
      useSip.setState({ call: "IDLE" });
    }
  });
}

export async function enableSip(audio: HTMLAudioElement) {
  if (
    useSip.getState().connection === "CONNECTING" ||
    useSip.getState().connection === "READY"
  )
    return;
  await disableSip();
  const attempt = ++generation;
  remote = audio;
  useSip.setState({ connection: "CONNECTING", error: null });
  try {
    const permission = await navigator.mediaDevices.getUserMedia({
      audio: true,
      video: false,
    });
    for (const track of permission.getTracks()) track.stop();
    const config = await api<{
      uri: string;
      username: string;
      password: string;
      websocket_path: string;
    }>("/sip/session");
    if (attempt !== generation) return;
    const ua = new UserAgent({
      uri: UserAgent.makeURI(config.uri),
      authorizationUsername: config.username,
      authorizationPassword: config.password,
      displayName: "Учебное рабочее место",
      logLevel: "error",
      logBuiltinEnabled: false,
      transportOptions: {
        server: `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${config.websocket_path}`,
      },
      sessionDescriptionHandlerFactoryOptions: {
        constraints: { audio: true, video: false },
        peerConnectionConfiguration: { iceServers: [] },
        iceGatheringTimeout: 500,
      },
      delegate: {
        onInvite: (invitation) => {
          useSip.setState({ diagnostic: false });
          if (session) {
            void invitation.reject({ statusCode: 486 });
            return;
          }
          attach(invitation);
          useSip.setState({ call: "RINGING", error: null });
        },
        onConnect: () => {
          if (registerer) void registerer.register().catch(fail);
        },
        onDisconnect: () => {
          if (agent !== ua) return;
          useSip.setState({
            connection: "ERROR",
            error: "Связь с телефоном потеряна. Подключаемся снова…",
          });
          const retry = () => {
            if (agent !== ua) return;
            void ua.reconnect().catch(() => {
              reconnectTimer = setTimeout(retry, 2000);
            });
          };
          reconnectTimer = setTimeout(retry, 2000);
        },
      },
    });
    agent = ua;
    registerer = new Registerer(ua, { expires: 90 });
    registerer.stateChange.addListener((state) => {
      if (agent !== ua) return;
      if (state === RegistererState.Registered)
        useSip.setState({ connection: "READY", error: null });
      else if (state === RegistererState.Unregistered)
        useSip.setState({
          connection: "ERROR",
          error: "Телефон не зарегистрирован. Повторите подключение.",
        });
    });
    await ua.start();
  } catch (error) {
    if (attempt === generation) {
      useSip.setState({ connection: "ERROR" });
      fail(error);
    }
  }
}

export async function disableSip() {
  generation++;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  const previous = agent;
  agent = null;
  registerer = null;
  if (previous) await previous.stop().catch(fail);
  session = null;
  if (remote) remote.srcObject = null;
  useSip.setState({ connection: "OFF", call: "IDLE", error: null });
}

export async function answerSip() {
  if (!(session instanceof Invitation)) return;
  try {
    await session.accept();
  } catch (error) {
    fail(error);
  }
}

export async function dialSip(uri: string) {
  if (!agent || useSip.getState().connection !== "READY")
    throw new Error("Включите гарнитуру.");
  if (session) throw new Error("Завершите текущий разговор.");
  const target = UserAgent.makeURI(uri);
  if (!target || target.host !== "dispatcher112")
    throw new Error("Допустим только учебный номер.");
  const next = new Inviter(agent, target);
  useSip.setState({ diagnostic: target.user === "9000" });
  attach(next);
  useSip.setState({ call: "DIALING", error: null });
  try {
    await next.invite({
      requestDelegate: {
        onReject: (response) => {
          useSip.setState({
            error: `Учебный вызов отклонён: ${response.message.statusCode}. Повторите вызов.`,
          });
        },
      },
    });
  } catch (error) {
    session = null;
    useSip.setState({ call: "IDLE" });
    throw error;
  }
}

export async function hangupSip() {
  if (!session) return;
  try {
    if (session.state === SessionState.Established) await session.bye();
    else if (session instanceof Invitation) await session.reject();
    else if (session instanceof Inviter) await session.cancel();
  } catch (error) {
    fail(error);
  }
}

export function sipConnection(): RTCPeerConnection | null {
  return (
    (
      session?.sessionDescriptionHandler as
        Web.SessionDescriptionHandler | undefined
    )?.peerConnection ?? null
  );
}
