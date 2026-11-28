package fr.rbb.tableaudebord

import android.app.Application
import fr.rbb.tableaudebord.auth.GoogleAuth
import fr.rbb.tableaudebord.data.Depot
import fr.rbb.tableaudebord.data.TdbBase
import fr.rbb.tableaudebord.net.GoogleApi

/** Point d'entrée : crée une seule fois la base chiffrée, l'accès Google et le dépôt. */
class TdbApp : Application() {

    val auth: GoogleAuth by lazy { GoogleAuth() }
    val api: GoogleApi by lazy { GoogleApi { auth.jeton } }
    private val base: TdbBase by lazy { TdbBase.ouvrir(this) }
    val depot: Depot by lazy { Depot(this, base.dao()) }
}
