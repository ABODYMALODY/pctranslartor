package com.gametranslator.tv

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Base64
import android.util.Log
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.SecureRandom
import java.util.UUID

/** One picture to draw: position and size are fractions of the screen (0..1). */
class Layer(val x: Float, val y: Float, val w: Float, val h: Float, val bitmap: Bitmap)

/**
 * Talks to the Game Translator program on the PC.
 *
 * Discovery (UDP 47800): the PC broadcasts "GT_DISCOVER <code>"; if the code is ours we answer
 * "GT_HERE {json}" so the PC learns our address.
 * Link (TCP 47801): one JSON object per line. The first line is "hello" with the code (first time)
 * or a token we gave earlier. Then "group" messages carry the pictures to draw.
 */
class LinkServer(private val context: Context, private val listener: Listener) {

    interface Listener {
        fun onConnected(pcName: String)
        fun onDisconnected()
        fun onGroup(id: String, layers: List<Layer>)
        fun onClearAll()
    }

    companion object {
        const val DISCOVERY_PORT = 47800
        const val LINK_PORT = 47801
        private const val TAG = "GameTranslatorLink"
        private const val PREFS = "link"
        private const val KEY_TOKENS = "tokens"
    }

    val code: String = String.format("%06d", SecureRandom().nextInt(1_000_000))
    val tvName: String = deviceName()

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var running = false
    @Volatile private var client: Socket? = null
    private var serverSocket: ServerSocket? = null
    private var udpSocket: DatagramSocket? = null
    private var multicastLock: WifiManager.MulticastLock? = null

    fun start() {
        if (running) return
        running = true
        // Some TVs drop broadcast packets to save power unless an app asks for them.
        val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
        multicastLock = wifi?.createMulticastLock("game-translator")?.apply {
            setReferenceCounted(false)
            acquire()
        }
        Thread({ discoveryLoop() }, "gt-discovery").start()
        Thread({ acceptLoop() }, "gt-accept").start()
    }

    fun stop() {
        running = false
        closeQuietly { udpSocket?.close() }
        closeQuietly { serverSocket?.close() }
        closeQuietly { client?.close() }
        multicastLock?.let { if (it.isHeld) it.release() }
    }

    fun disconnectClient() {
        closeQuietly { client?.close() }
    }

    // ------------------------------------------------------------------ discovery

    private fun discoveryLoop() {
        try {
            val socket = DatagramSocket(null).apply {
                reuseAddress = true
                broadcast = true
                bind(InetSocketAddress(DISCOVERY_PORT))
            }
            udpSocket = socket
            val buffer = ByteArray(2048)
            while (running) {
                val packet = DatagramPacket(buffer, buffer.size)
                socket.receive(packet)
                val text = String(packet.data, 0, packet.length, Charsets.UTF_8).trim()
                if (text == "GT_DISCOVER $code") {
                    val info = JSONObject()
                        .put("code", code)
                        .put("port", LINK_PORT)
                        .put("name", tvName)
                    val reply = "GT_HERE $info".toByteArray(Charsets.UTF_8)
                    socket.send(DatagramPacket(reply, reply.size, packet.address, packet.port))
                }
            }
        } catch (e: Exception) {
            if (running) Log.w(TAG, "Discovery stopped", e)
        }
    }

    // ------------------------------------------------------------------ link

    private fun acceptLoop() {
        try {
            val server = ServerSocket().apply {
                reuseAddress = true
                bind(InetSocketAddress(LINK_PORT))
            }
            serverSocket = server
            while (running) {
                val socket = server.accept()
                Thread({ handle(socket) }, "gt-client").start()
            }
        } catch (e: Exception) {
            if (running) Log.w(TAG, "Link server stopped", e)
        }
    }

    private fun handle(socket: Socket) {
        var accepted = false
        try {
            socket.tcpNoDelay = true
            socket.soTimeout = 10_000
            val reader = BufferedReader(InputStreamReader(socket.getInputStream(), Charsets.UTF_8), 1 shl 16)
            val writer = OutputStreamWriter(socket.getOutputStream(), Charsets.UTF_8)

            val hello = JSONObject(reader.readLine() ?: return)
            val token = hello.optString("token")
            val knownToken = token.isNotEmpty() && knownTokens().contains(token)
            val rightCode = hello.optString("code") == code
            if (hello.optString("type") != "hello" || !(knownToken || rightCode)) {
                writer.write(
                    JSONObject().put("type", "refused")
                        .put("reason", "Wrong code. Type the code shown on the TV right now.").toString() + "\n"
                )
                writer.flush()
                return
            }
            val newToken = if (knownToken) token else UUID.randomUUID().toString().also { saveToken(it) }
            writer.write(JSONObject().put("type", "welcome").put("token", newToken).put("tv", tvName).toString() + "\n")
            writer.flush()

            // One PC at a time: the newest connection wins.
            val old = client
            client = socket
            if (old != null && old !== socket) closeQuietly { old.close() }
            accepted = true
            socket.soTimeout = 15_000 // the PC sends a ping every 2 seconds
            val pcName = hello.optString("pc", "PC")
            main.post { listener.onConnected(pcName) }

            while (running) {
                val line = reader.readLine() ?: break
                if (line.isBlank()) continue
                val message = JSONObject(line)
                when (message.optString("type")) {
                    "group" -> {
                        val id = message.getString("id")
                        val layers = decodeLayers(message) // decode pictures here, not on the UI thread
                        main.post { listener.onGroup(id, layers) }
                    }
                    "clear_all" -> main.post { listener.onClearAll() }
                }
            }
        } catch (e: Exception) {
            Log.i(TAG, "Connection closed: ${e.message}")
        } finally {
            closeQuietly { socket.close() }
            if (accepted && client === socket) {
                client = null
                main.post { listener.onDisconnected() }
            }
        }
    }

    private fun decodeLayers(message: JSONObject): List<Layer> {
        val array = message.optJSONArray("layers") ?: return emptyList()
        val layers = ArrayList<Layer>(array.length())
        for (i in 0 until array.length()) {
            val item = array.getJSONObject(i)
            val bytes = Base64.decode(item.getString("png"), Base64.DEFAULT)
            val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: continue
            layers.add(
                Layer(
                    item.getDouble("x").toFloat(),
                    item.getDouble("y").toFloat(),
                    item.getDouble("w").toFloat(),
                    item.getDouble("h").toFloat(),
                    bitmap,
                )
            )
        }
        return layers
    }

    // ------------------------------------------------------------------ helpers

    private fun knownTokens(): Set<String> =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getStringSet(KEY_TOKENS, emptySet()) ?: emptySet()

    private fun saveToken(token: String) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val tokens = HashSet(knownTokens())
        tokens.add(token)
        prefs.edit().putStringSet(KEY_TOKENS, tokens).apply()
    }

    private fun deviceName(): String =
        Settings.Global.getString(context.contentResolver, Settings.Global.DEVICE_NAME) ?: Build.MODEL ?: "TV"

    private inline fun closeQuietly(block: () -> Unit) {
        try {
            block()
        } catch (_: Exception) {
        }
    }
}
