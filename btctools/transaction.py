from copy import deepcopy, copy
from collections import deque
from time import sleep

from transformations import int_to_bytes, bytes_to_int, bytes_to_hex, hex_to_bytes, sha256
from btctools.opcodes import SIGHASH, TX
from btctools.script import VM, asm, witness_program, push, pad, ScriptValidationError, var_int, serialize, depush
from ECDS.secp256k1 import PrivateKey, CURVE


concat = b''.join


class TransactionError(Exception):
    def __init__(self, message, tx=None, data=None, txhash=None):
        self.message = message
        self.tx = tx
        self.data = data
        self.txhash = txhash


class SerializationError(TransactionError):
    pass


class ValidationError(TransactionError):
    pass


class Input:
    def __init__(self, output, index, script, sequence=b'\xff\xff\xff\xff', witness=None, referenced_tx=None):
        # Parameters should be bytes as transmitted i.e reversed
        assert isinstance(output, bytes) and len(output) == 32
        self.output = output[::-1]  # referenced tx hash
        self.index = index[::-1] if isinstance(index, int) else bytes_to_int(index[::-1])
        assert self.index <= 0xffffffff
        self.script = script
        self.sequence = sequence[::-1]
        self._referenced_tx = referenced_tx
        self._referenced_output = None
        self.witness = witness

    def ref(self):
        """The output that this input is spending"""
        if self._referenced_output is not None:
            return self._referenced_output
        if self._referenced_tx is None or self._referenced_tx.txid()[::-1] != self.output:
            # Gets the transaction from bitcoin.info and caches the result
            self._referenced_tx = Transaction.get(self.output)
        return self._referenced_tx.outputs[self.index]

    @property
    def segwit(self):
        return bool(self.witness)

    @property
    def sequence(self):
        return bytes_to_int(self._sequence)

    @sequence.setter
    def sequence(self, x):
        self._sequence = pad(x, 4)

    @property
    def index(self):
        return bytes_to_int(self._index)

    @index.setter
    def index(self, x):
        self._index = pad(x, 4)

    def serialize(self):
        return self.output[::-1] + self._index[::-1] + serialize(self.script) + self._sequence[::-1]

    def serialize_witness(self):
        if not self.segwit:
            return b'\x00'
        result = int_to_bytes(len(self.witness))
        for stack_item in self.witness:
            result += int_to_bytes(len(stack_item)) + stack_item
        return result

    def outpoint(self):
        return self.output[::-1] + self._index[::-1]

    def is_nested(self):
        if self.ref().type() == TX.P2SH:
            try:
                witness_script = witness_program(depush(self.script))
            except ScriptValidationError:
                return False
            if len(witness_script) == 20:
                return TX.P2WPKH
            elif len(witness_script) == 32:
                return TX.P2WSH
        return False

    def type(self):
        referenced_output_type = self.ref().type()
        my_type = str(referenced_output_type.value)
        nested = self.is_nested()
        if nested:
            my_type += f"-{nested.value}"
        return my_type

    def scriptcode(self):
        output = self.ref()
        output_type = self.ref().type()
        if output_type == TX.P2WPKH:
            # OP_DUP OP_HASH160 <pubKeyHash> OP_EQUALVERIFY OP_CHECKSIG
            return b'\x76\xa9' + push(witness_program(output.script)) + b'\x88\xac'
        elif output_type == TX.P2SH:
            if self.is_nested() == TX.P2WPKH:
                return b'\x76\xa9' + push(witness_program(self.script[1:])) + b'\x88\xac'
            elif self.is_nested() == TX.P2WSH:
                return self.witness[-1]
        # elif output_type == TX.P2WPKH:
        #     return output.script
        # elif output_type == TX.P2SH:
        #     return self.script
        elif output_type == TX.P2WSH:
            return self.witness[-1]
        else:
            raise ScriptValidationError(f"No scriptcode for {output_type}")
    @classmethod
    def deserialize(cls, bts):
        output, index, script_len = bts[:32], bts[32:36], bts[36:37]
        script_end = 37 + bytes_to_int(script_len)
        script, sequence = bts[37:script_end], bts[script_end:]
        assert len(sequence) == 4, 'Invalid input format'
        return cls(output=output, index=index, script=script, sequence=sequence)

    def __repr__(self):
        return f"{self.__class__.__name__}(from={bytes_to_hex(self.output)})"

    def asm(self):
        return asm(self.script)

    def json(self):
        result = {
            "txid": bytes_to_hex(self.output),
            "vout": self.index,
            "scriptSig": {
                "hex": bytes_to_hex(self.script)
            }
        }
        if self.segwit:
            result['witness'] = [bytes_to_hex(wit) for wit in self.witness]
        result["sequence"] = self.sequence
        return result


class Output:

    def __init__(self, value, script):
        # Parameters should be bytes as transmitted i.e reversed
        if isinstance(value, bytes):
            assert len(value) == 8
            self.value = value[::-1]
        else:
            self.value = value

        self.script = script

    @property
    def value(self):
        return bytes_to_int(self._value)

    @value.setter
    def value(self, x):
        self._value = pad(x, 8)

    def serialize(self):
        return self._value[::-1] + serialize(self.script)

    @classmethod
    def deserialize(cls, bts):
        value, script_len = bts[:8], bts[8:9]
        script_end = 9 + bytes_to_int(script_len)
        script, rest = bts[9:script_end], bts[script_end:]
        assert not rest, 'Invalid output format'
        return cls(value=value, script=script)

    def asm(self):
        return asm(self.script)

    def type(self):
        """https://github.com/bitcoin/bitcoin/blob/5961b23898ee7c0af2626c46d5d70e80136578d3/src/script/script.cpp#L202"""
        if self.script.startswith(b'\xa9\x14') and self.script.endswith(b'\x87') and len(self.script) == 23:
            return TX.P2SH
        elif self.script.startswith(b'\x76\xa9') and self.script.endswith(b'\x88\xac') and len(self.script) == 25:
            return TX.P2PKH
        elif self.script.startswith(b'\x00\x20') and len(self.script) == 34:
            return TX.P2WSH
        elif self.script.startswith(b'\x00\x14') and len(self.script) == 22:
            return TX.P2WPKH
        elif self.script.startswith(b'\x41\x04') and self.script.endswith(b'\xac') and len(self.script) == 67:  # uncompressed PK
            return TX.P2PK
        elif self.script.startswith((b'\x21\x03', b'\x21\x02')) and self.script.endswith(b'\xac') and len(self.script) == 35:  # compressed PK
            return TX.P2PK
        else:
            raise ValidationError(f"Unknown output type: {bytes_to_hex(self.script)}")

    def __repr__(self):
        return f"{self.__class__.__name__}(type={self.type()}, value={self.value/10**8} BTC)"

    def json(self, index=None):
        data = {
            "value": self.value/10**8,
        }
        if index:
            data["n"] = index
        data["scriptPubKey"] = {
            "hex": bytes_to_hex(self.script),
            "asm": self.asm()
        }
        return data


class Transaction:

    def __init__(self, inputs, outputs, version=b'\x01\x00\x00\x00', lock_time=b'\x00\x00\x00\x00'):
        self.inputs = inputs
        self.outputs = outputs
        assert len(version) == 4, 'Invalid Version'
        assert len(lock_time) == 4, 'Invalid lock time'
        self._version = version[::-1]
        self.version = bytes_to_int(self._version)
        self._lock_time = lock_time[::-1]
        self.lock_time = bytes_to_int(self._lock_time)

    def __len__(self):
        return len(self.serialize())

    @property
    def segwit(self):
        """https://github.com/bitcoin/bips/blob/master/bip-0141.mediawiki#specification"""
        return any((inp.segwit for inp in self.inputs))

    def serialize(self, segwit=None):
        if segwit is None:
            segwit = self.segwit
        inputs = concat((inp.serialize() for inp in self.inputs))
        outputs = concat((out.serialize() for out in self.outputs))
        if not segwit:
            return self._version[::-1] + var_int(len(self.inputs)) + inputs + var_int(len(self.outputs)) + outputs + self._lock_time[::-1]
        witness = concat((inp.serialize_witness() for inp in self.inputs))
        return self._version[::-1] + b'\x00\x01' + var_int(len(self.inputs)) + inputs + var_int(len(self.outputs)) + outputs + witness + self._lock_time[::-1]

    @classmethod
    def deserialize(cls, tx: bytes) -> 'Transaction':
        original_tx = copy(tx)
        try:
            return cls._deserialize(tx)
        except AssertionError as e:
            raise SerializationError(str(e), data=original_tx) from None

    @classmethod
    def _deserialize(cls, tx: bytes) -> 'Transaction':
        segwit = False
        tx = deque(tx)
        def pop(x):
            # nonlocal tx
            # data = tx[:x]
            data = []
            for _ in range(x):
                data.append(tx.popleft())
            # assert data or x == 0, 'EOF'
            # tx = tx[x:]
            return bytes(data)

        def read_var_int():
            """https://en.bitcoin.it/wiki/Protocol_documentation#Variable_length_integer"""
            byte = pop(1)
            if byte == b'\xfd':
                result = pop(2)
            elif byte == b'\xfe':
                result = pop(4)
            elif byte == b'\xff':
                result = pop(8)
            else:
                result = byte
            return bytes_to_int(result[::-1])

        version = pop(4)
        input_count = read_var_int()
        if input_count == 0x00:
            segwit = True

            flag = pop(1)
            assert flag == b'\x01'
            input_count = read_var_int()

        inputs, outputs, witnesses = [], [], []

        for _ in range(input_count):
            tx_hash = pop(32)
            index = pop(4)
            script_len = read_var_int()
            script = pop(script_len)
            sequence = pop(4)

            inp = Input(output=tx_hash, index=index, script=script, sequence=sequence, witness=None)
            inputs.append(inp)

        output_count = read_var_int()

        for _ in range(output_count):
            value = pop(8)
            script_len = read_var_int()
            script = pop(script_len)

            out = Output(value=value, script=script)
            outputs.append(out)

        if segwit:
            for inp in inputs:
                len_witness = read_var_int()

                elements = []
                for _ in range(len_witness):
                    element_len = read_var_int()
                    element = pop(element_len)
                    elements.append(element)

                if elements:
                    inp.witness = tuple(elements)

        lock_time = pop(4)
        assert not tx, f"{len(tx)} Leftover bytes"
        return cls(inputs=inputs, outputs=outputs, version=version, lock_time=lock_time)

    def __repr__(self):
        return f"{self.__class__.__name__}(inputs={len(self.inputs)}, outputs={len(self.outputs)})"

    def txid(self):
        return sha256(sha256(self.serialize(segwit=False)))

    def wtxid(self):
        if not self.segwit:
            return self.txid()
        return sha256(sha256(self.serialize(segwit=True)))

    def json(self):
        return {
            "txid": bytes_to_hex(self.txid()[::-1]),  # TODO
            "version": self.version,
            "size": len(self.serialize()),
            "locktime": self.lock_time,
            "vin": [inp.json() for inp in self.inputs],
            "vout": [out.json(i) for i, out in enumerate(self.outputs)]
        }

    def hex(self):
        return bytes_to_hex(self.serialize())

    @classmethod
    def from_hex(cls, hexstring):
        return cls.deserialize(hex_to_bytes(hexstring))

    @classmethod
    def get(cls, txhash):
        import urllib.request
        if isinstance(txhash, bytes):
            txhash = bytes_to_hex(txhash)
        url = f"https://blockchain.info/rawtx/{txhash}?format=hex"
        req = urllib.request.Request(url)
        sleep(0.1)
        with urllib.request.urlopen(req) as resp:
            assert 200 <= resp.status < 300, f"{resp.status}: {resp.reason}"
            try:
                return cls.from_hex(resp.read().decode())
            except SerializationError as e:
                e.txhash = txhash
                raise e

    def sighash(self, i, hashcode=SIGHASH.ALL):
        inp = self.inputs[i]
        tx_type = inp.ref().type()

        if tx_type in (TX.P2PK, TX.P2PKH) or (tx_type == TX.P2SH and not inp.is_nested()):
            preimage = self.signature_form_legacy(i=i, hashcode=hashcode)
        else:
            preimage = self.signature_form_segwit(i=i, hashcode=hashcode)

        return sha256(sha256(preimage))

    def signature_form_legacy(self, i, script=None, hashcode=SIGHASH.ALL):
        """Create the object to be signed for the i-th input of this transaction"""
        # Recreates the object that needs to be signed which is not the actual transaction
        # More info at:
        # https://bitcoin.stackexchange.com/questions/32628/redeeming-a-raw-transaction-step-by-step-example-required/32695#32695
        # https://bitcoin.stackexchange.com/questions/3374/how-to-redeem-a-basic-tx
        # https://rya.nc/sartre.html
        # https://bitcoin.stackexchange.com/questions/41209/how-to-sign-a-transaction-with-multiple-inputs
        # https://en.bitcoin.it/wiki/OP_CHECKSIG
        tx = deepcopy(self)

        # the input references a previous output from which we need the scriptPubKey
        # if it was not provided get it from blockchain.info
        if script is None:
            script = tx.inputs[i].ref().script

        for input in tx.inputs:
            input.script = b''

        tx.inputs[i].script = script

        # https://en.bitcoin.it/wiki/OP_CHECKSIG
        if hashcode == SIGHASH.NONE:
            tx.outputs = []
        elif hashcode == SIGHASH.SINGLE:
            tx.outputs = tx.outputs[:len(tx.inputs)]
            for output in tx.outputs:
                output.script = b''
                output.value = 2**64 - 1
            for idx, input in enumerate(tx.inputs):
                if not idx == i:
                    input.sequence = 0
        elif hashcode == SIGHASH.ANYONECANPAY:
            tx.inputs = [tx.inputs[i]]

        return tx.serialize(segwit=tx.inputs[i].segwit) + pad(hashcode.value, 4)[::-1]

    def signature_form_segwit(self, i, hashcode=SIGHASH.ALL):
        """Segwit version of signature_form"""
        # https://github.com/bitcoin/bips/blob/master/bip-0143.mediawiki#specification
        tx = deepcopy(self)

        ref = tx.inputs[i].ref()

        nversion = tx._version[::-1]
        hashprevouts = sha256(sha256(concat((inp.outpoint() for inp in tx.inputs)))) if not hashcode.is_anyonecanpay() else pad(0, 32)
        hashsequence = sha256(sha256(concat(inp._sequence[::-1] for inp in tx.inputs))) if hashcode == SIGHASH.ALL else pad(0, 32)
        outpoint = tx.inputs[i].outpoint()
        scriptcode = serialize(tx.inputs[i].scriptcode())
        value = ref._value[::-1]
        nsequence = tx.inputs[i]._sequence[::-1]

        if not (hashcode.is_single() or hashcode.is_none()):
            hashoutputs = sha256(sha256(concat(out._value[::-1] + push(out.script) for out in tx.outputs)))
        elif hashcode.is_single() and i < len(tx.outputs):
            hashoutputs = sha256(sha256(tx.outputs[i]._value[::-1] + push(tx.outputs[i].script)))
        else:
            hashoutputs = pad(0, 32)

        nlocktime = tx._lock_time[::-1]
        sighash = pad(hashcode.value, 4)[::-1]

        return concat([nversion, hashprevouts, hashsequence, outpoint, scriptcode, value, nsequence, hashoutputs, nlocktime, sighash])

    def sign_input(self, i: int, private: PrivateKey, hashcode=SIGHASH.ALL):
        """Sign the i-th input"""
        digest = self.sighash(i=i, hashcode=hashcode)
        sig = private.sign_hash(digest)
        # https://github.com/bitcoin/bips/blob/master/bip-0146.mediawiki#low_s
        if sig.s > CURVE.N//2:
            sig.s = CURVE.N - sig.s
        return sig

    def verify(self, i=None):
        """Run the script for the i-th input or all the inputs"""
        if i is not None:
            vm = VM(self, i)
            return vm.verify()
        else:
            results = []
            for idx in range(len(self.inputs)):
                vm = VM(self, idx)
                results.append(vm.verify())

            return all(results)

