import asyncio
from time import time
from grader.tcputils import FLAGS_ACK, FLAGS_FIN, FLAGS_SYN, MSS, fix_checksum, make_header
from tcputils import *


class Servidor:
    def __init__(self, rede, porta):
        self.rede = rede
        self.porta = porta
        self.conexoes = {}
        self.callback = None
        self.rede.registrar_recebedor(self._rdt_rcv)

    def registrar_monitor_de_conexoes_aceitas(self, callback):
        self.callback = callback

    def _rdt_rcv(self, src_addr, dst_addr, segment):
        src_port, dst_port, seq_no, ack_no, flags, window_size, checksum, urg_ptr = read_header(segment)

        if dst_port != self.porta:
            return
        if not self.rede.ignore_checksum and calc_checksum(segment, src_addr, dst_addr) != 0:
            print('descartando segmento com checksum incorreto')
            return

        payload_data = segment[4*(flags >> 12):]
        id_conexao = (src_addr, src_port, dst_addr, dst_port)

        if (flags & FLAGS_SYN) == FLAGS_SYN:
            ack_no = seq_no + 1
            conexao = self.conexoes[id_conexao] = Conexao(self, id_conexao, seq_no, ack_no)
            response_flags = FLAGS_SYN + FLAGS_ACK
            resposta_segmento = fix_checksum(make_header(dst_port, src_port, seq_no, ack_no, response_flags), src_addr, dst_addr)
            self.rede.enviar(resposta_segmento, src_addr)
            if self.callback:
                self.callback(conexao)
        elif id_conexao in self.conexoes:
            self.conexoes[id_conexao]._rdt_rcv(seq_no, ack_no, flags, payload_data)
        else:
            print('%s:%d -> %s:%d (pacote associado a conexão desconhecida)' %
                  (src_addr, src_port, dst_addr, dst_port))


class Conexao:
    def __init__(self, servidor, id_conexao, seq_no, ack_no):
        self.servidor = servidor
        self.id_conexao = id_conexao
        self.callback = None
        self.timer = None

        self.seq_no = seq_no
        self.ack_no = ack_no
        self.ack_client = ack_no
        self.seq_client = ack_no

        self.dados_enviados = {}
        self.segmentos = {}
        self.intervalo_timeout = 1
        self.exemplo_rtt = 0
        self.DevRTT = 0
        self.rtt_estimado = 0
        self.tempo_envio = 0
        self.rcv_cwnd = 0
        self.cwnd = MSS
        self.open = True
        self.reenvio = False
        

    def _timer(self):
        self.reenvio = True
        self.cwnd = ((self.cwnd // MSS) // 2) * MSS
        self.enviar(self.dados_enviados[list(self.dados_enviados.keys())[0]])

    def _rdt_rcv(self, seq_no, ack_no, flags, payload):
        if len(self.dados_enviados):
            if not self.reenvio:
                first = self.exemplo_rtt == 0
                self.exemplo_rtt = time() - self.tempo_envio
                if first:
                    self.rtt_estimado = self.exemplo_rtt
                    self.DevRTT = self.exemplo_rtt / 2
                else:
                    self.rtt_estimado = 0.875 * self.rtt_estimado + 0.125 * self.exemplo_rtt
                    self.DevRTT = 0.75 * self.DevRTT + 0.25 * abs(self.exemplo_rtt - self.rtt_estimado)
                self.intervalo_timeout = self.rtt_estimado + 4 * self.DevRTT

            if ack_no > list(self.dados_enviados.keys())[0]:
                temp = list(self.dados_enviados.keys())[0]
                while temp < ack_no:
                    self.rcv_cwnd += len(self.segmentos[temp])
                    del self.dados_enviados[temp]
                    del self.segmentos[temp]
                    if len(self.dados_enviados) == 0:
                        break
                    temp = list(self.dados_enviados.keys())[0]

                if len(self.dados_enviados):
                    if self.timer is not None:
                        self.timer.cancel()
                    self.timer = asyncio.get_event_loop().call_later(self.intervalo_timeout, self._timer)
                else:
                    self.timer.cancel()

                if self.rcv_cwnd >= self.cwnd or len(self.dados_enviados) == 0:
                    self.cwnd += MSS
                    self.rcv_cwnd = 0
                    if len(self.dados_enviados):
                        if self.timer is not None:
                            self.timer.cancel()
                        self.timer = asyncio.get_event_loop().call_later(self.intervalo_timeout, self._timer)
                        self.enviar(self.dados_enviados[list(self.dados_enviados.keys())[0]])

        self.reenvio = False

        if seq_no != self.ack_no or (len(payload) == 0 and (flags & FLAGS_FIN) != FLAGS_FIN) or not self.open:
            return

        src_addr, src_port, dst_addr, dst_port = self.id_conexao
        self.seq_no = self.ack_no
        self.ack_no += len(payload)
        if (flags & FLAGS_FIN) == FLAGS_FIN:
            self.ack_no += 1
        self.ack_client = self.ack_no
        flags = FLAGS_ACK
        self.callback(self, payload)
        new_segment = fix_checksum(make_header(dst_port, src_port, self.seq_no, self.ack_no, flags), src_addr, dst_addr)
        self.servidor.rede.enviar(new_segment, src_addr)

        if (flags & FLAGS_FIN) == FLAGS_FIN:
            self.fechar()

    def registrar_recebedor(self, callback):
        self.callback = callback

    def enviar(self, dados):
        if not self.open:
            return

        src_addr, src_port, dst_addr, dst_port = self.id_conexao
        index = 0

        if len(self.dados_enviados) == 0:
            while index < len(dados):
                payload = dados[index:index + MSS]
                flags = FLAGS_ACK
                self.dados_enviados[self.seq_client] = payload
                segmento_novo = fix_checksum(make_header(dst_port, src_port, self.seq_client, self.ack_no, flags) + payload, src_addr, dst_addr)
                self.segmentos[self.seq_client] = segmento_novo
                index += MSS
                self.seq_client += len(payload)

        contador = 0
        if not self.reenvio:
            for seq in self.dados_enviados.keys():
                if contador >= self.cwnd:
                    break
                self.servidor.rede.enviar(self.segmentos[seq], src_addr)
                contador += len(self.segmentos[seq])
        else:
            primeiro_item = list(self.dados_enviados.keys())[0]
            self.servidor.rede.enviar(self.segmentos[primeiro_item], src_addr)

        self.tempo_envio = time()
        if self.timer is not None:
            self.timer.cancel()
        self.timer = asyncio.get_event_loop().call_later(self.intervalo_timeout, self._timer)

    def fechar(self):
        self.callback(self, b'')
        src_addr, src_port, dst_addr, dst_port = self.id_conexao
        self.open = False
        flags = FLAGS_FIN
        segmento_novo = fix_checksum(make_header(dst_port, src_port, self.seq_no, self.ack_no, flags), src_addr, dst_addr)
        self.servidor.rede.enviar(segmento_novo, src_addr)
