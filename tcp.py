import asyncio
from tcputils import *
from random import randint 


class Servidor:
    def __init__(self, rede, porta):
        self.rede = rede
        self.porta = porta
        self.conexoes = {} # Dicionario de conexoes
        self.callback = None
        self.rede.registrar_recebedor(self._rdt_rcv)

    def registrar_monitor_de_conexoes_aceitas(self, callback):
        """
        Usado pela camada de aplicação para registrar uma função para ser chamada
        sempre que uma nova conexão for aceita
        """
        self.callback = callback

    def _rdt_rcv(self, src_addr, dst_addr, segment):
        src_port, dst_port, seq_no, ack_no, \
            flags, window_size, checksum, urg_ptr = read_header(segment)

        if dst_port != self.porta:
            # Ignora segmentos que não são destinados à porta do nosso servidor
            return
        if not self.rede.ignore_checksum and calc_checksum(segment, src_addr, dst_addr) != 0:
            print('descartando segmento com checksum incorreto')
            return

        payload = segment[4*(flags>>12):]
        id_conexao = (src_addr, src_port, dst_addr, dst_port)

        if (flags & FLAGS_SYN) == FLAGS_SYN:
            # A flag SYN estar setada significa que é um cliente tentando estabelecer uma conexão nova
            # TODO: talvez você precise passar mais coisas para o construtor de conexão
            conexao = self.conexoes[id_conexao] = Conexao(self, id_conexao)
            # TODO: você precisa fazer o handshake aceitando a conexão. Escolha se você acha melhor
            # fazer aqui mesmo ou dentro da classe Conexao.
            conexao.seq_no = randint(0, 0xffff)
            conexao.ack_no = seq_no + 1


            src_addr, dst_addr = dst_addr, src_addr
            src_port, dst_port = dst_port, src_port

            # src_port, dst_port, seq_no, ack_no, flags
            segment_flags = fix_checksum(make_header(src_port, dst_port, conexao.seq_no, conexao.ack_no, FLAGS_SYN | FLAGS_ACK), src_addr, dst_addr)


            self.rede.enviar(segment_flags, dst_addr)

            conexao.seq_no += 1
            conexao.base_sqe_no = conexao.seq_no

            if self.callback:
                self.callback(conexao)
        elif id_conexao in self.conexoes:
            # Passa para a conexão adequada se ela já estiver estabelecida
            self.conexoes[id_conexao]._rdt_rcv(seq_no, ack_no, flags, payload)
        else:
            print('%s:%d -> %s:%d (pacote associado a conexão desconhecida)' %
                  (src_addr, src_port, dst_addr, dst_port))

import asyncio
from time import time

FLAGS_ACK = 0x10
FLAGS_FIN = 0x01

class Conexao:
    def __init__(self, servidor, id_conexao):
        self.servidor = servidor
        self.id_conexao = id_conexao
        self.seq_no = None
        self.ack_no = None
        self.callback = None
        self.timer = None
        self.base_seq_no = None
        self.seg_no_ack = []
        self.timeoutInterval = 1
        self.desvioRTT = None
        self.RTTestimado = None
        

    def _timer(self):
        if self.seg_no_ack:
            segmento, _, dst_addr, _ = self.seg_no_ack[0]
            self.servidor.rede.enviar(segmento, dst_addr)
            self.seg_no_ack[0][3] = None

    def _rdt_rcv(self, seq_no, ack_no, flags, payload):
        if seq_no != self.ack_no:
            return

        if (flags & FLAGS_ACK) == FLAGS_ACK and ack_no > self.base_sqe_no:
            self._process_ack(ack_no)

        if (flags & FLAGS_FIN) == FLAGS_FIN: # Passo 4
            self._process_fin()
        elif len(payload) > 0:
            self._process_payload(payload)

    def _process_ack(self, ack_no):
        self.base_sqe_no = ack_no
        if self.seg_no_ack:
            if not self.seg_no_ack or self.seg_no_ack[0][3] is None:
                return

            _, _, _, sampleRTT = self.seg_no_ack[0]
            sampleRTT = round(time(), 5) - sampleRTT

            if self.RTTestimado is None:
                self.RTTestimado = sampleRTT
                self.desvioRTT = sampleRTT / 2
            else:
                self.RTTestimado = 0.875 * self.RTTestimado + 0.125 * sampleRTT
                self.desvioRTT = 0.75 * self.desvioRTT + 0.25 * abs(sampleRTT - self.RTTestimado)
            self.timeoutInterval = self.RTTestimado + 4 * self.desvioRTT

            self.timer.cancel()
            self.seg_no_ack.pop(0)
            if self.seg_no_ack:
                self.timer = asyncio.get_event_loop().call_later(self.timeoutInterval, self._timer)

    def _process_fin(self):
        payload = b''
        self.ack_no += 1
        self._process_payload(payload)

    def _process_payload(self, payload):
        self.callback(self, payload)
        self.ack_no += len(payload)
        self._send_ack()

    def _send_ack(self):
        dst_addr, dst_port, src_addr, src_port = self.id_conexao
        segmento = make_header(src_port, dst_port, self.base_sqe_no, self.ack_no, FLAGS_ACK)
        segmento_checksum_corrigido = fix_checksum(segmento, src_addr, dst_addr)
        self.servidor.rede.enviar(segmento_checksum_corrigido, dst_addr)
    # Os métodos abaixo fazem parte da API

    def registrar_recebedor(self, callback):
        """
        Usado pela camada de aplicação para registrar uma função para ser chamada
        sempre que dados forem corretamente recebidos
        """
        self.callback = callback

    def enviar(self, dados):
        """
        Usado pela camada de aplicação para enviar dados
        """
        # TODO: implemente aqui o envio de dados.
        # Chame self.servidor.rede.enviar(segmento, dest_addr) para enviar o segmento
        # que você construir para a camada de rede.
 
        dst_addr, dst_port, src_addr, src_port = self.id_conexao 
        for i in range(int(len(dados)/MSS)):
            begin = i * MSS
            end_payload = min(len(dados), (i+1)*MSS) 

            payload = dados[begin:end_payload]

            # src_port, dst_port, seq_no, ack_no, flags
            segment_flags = fix_checksum(make_header(src_port, dst_port, self.seq_no, self.ack_no, 0 | FLAGS_ACK )+payload, src_addr, dst_addr)


            self.servidor.rede.enviar(segment_flags, dst_addr)
            self.timer = asyncio.get_event_loop().call_later(self.timeoutInterval, self._timer)
            self.seg_no_ack.append([segment_flags, len(payload), dst_addr, round(time(), 5)])
            self.seq_no += len(payload)

    # Funcao fechar eh chamada pela funcao dados_recebidos do arquivo exemplo_integracao.py
    def fechar(self):
        """
        Usado pela camada de aplicação para fechar a conexão
        """
        dst_addr, dst_port, src_addr, src_port = self.id_conexao

        segment_flags = fix_checksum(make_header(src_port, dst_port, self.seq_no, self.ack_no, FLAGS_FIN), src_addr, dst_addr)

        self.servidor.rede.enviar(segment_flags, dst_addr)
        # removendo essa conexao do dicionario de conexoes do servidor
        if self.id_conexao in self.servidor.conexoes:
            del self.servidor.conexoes[self.id_conexao]