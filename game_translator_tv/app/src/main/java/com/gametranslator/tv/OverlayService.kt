package com.gametranslator.tv

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * Keeps running after you leave the app (press Home, switch to the PlayStation's HDMI input),
 * listens for the PC and draws what it sends.
 */
class OverlayService : Service(), LinkServer.Listener {

    data class Status(val code: String, val address: String, val connectedTo: String?, val canDraw: Boolean)

    companion object {
        @Volatile var instance: OverlayService? = null
            private set
        /** Set by the main screen while it is visible. Always called on the main thread. */
        var statusListener: ((Status) -> Unit)? = null
    }

    private lateinit var link: LinkServer
    private lateinit var overlay: OverlayLayers
    private var connectedTo: String? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        startInForeground()
        overlay = OverlayLayers(this)
        overlay.attach()
        link = LinkServer(this, this)
        link.start()
        publish()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        overlay.attach() // the permission may have been given since
        publish()
        return START_STICKY
    }

    override fun onDestroy() {
        link.stop()
        overlay.detach()
        instance = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    fun status() = Status(link.code, localAddress(), connectedTo, Settings.canDrawOverlays(this))

    fun disconnect() = link.disconnectClient()

    // ------------------------------------------------------------------ LinkServer.Listener (main thread)

    override fun onConnected(pcName: String) {
        connectedTo = pcName
        overlay.attach()
        publish()
    }

    override fun onDisconnected() {
        connectedTo = null
        overlay.clearAll()
        publish()
    }

    override fun onGroup(id: String, layers: List<Layer>) = overlay.setGroup(id, layers)

    override fun onClearAll() = overlay.clearAll()

    // ------------------------------------------------------------------ helpers

    private fun publish() {
        statusListener?.invoke(status())
    }

    private fun startInForeground() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel("link", "Translations", NotificationManager.IMPORTANCE_LOW)
        )
        val notification = Notification.Builder(this, "link")
            .setContentTitle(getString(R.string.app_name))
            .setContentText("Ready to show translations")
            .setSmallIcon(R.drawable.icon)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, notification)
        }
    }

    private fun localAddress(): String {
        try {
            for (network in NetworkInterface.getNetworkInterfaces()) {
                if (!network.isUp || network.isLoopback) continue
                for (address in network.inetAddresses) {
                    if (address is Inet4Address && address.isSiteLocalAddress) return address.hostAddress ?: continue
                }
            }
        } catch (_: Exception) {
        }
        return "unknown"
    }
}
