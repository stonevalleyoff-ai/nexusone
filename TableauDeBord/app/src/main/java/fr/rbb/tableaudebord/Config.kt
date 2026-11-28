package fr.rbb.tableaudebord

/** Identifiants fixes de ton Drive et autorisations demandées à Google. */
object Config {
    /** Dossier Drive « Tableau de bord » : synthèses écrites par Claude + retours.json. */
    const val DOSSIER_TABLEAU_DE_BORD = "1VPyS80MAIqZa9JpLlSm5PZyxaHtxg7aL"

    /** Dossier Drive « Pièces jointes Gmail » : documents rangés par mois. */
    const val DOSSIER_PIECES_JOINTES = "1hjpan16e1q9PohbBuPOf7Xj6Wy5X9YoR"

    /** Préfixes des synthèses quotidiennes, une par boîte : synthese-gmail-AAAA-MM-JJ.json. */
    val BOITES = listOf("gmail", "laposte")
    fun prefixeSynthese(boite: String) = "synthese-$boite-"

    const val FICHIER_RETOURS = "retours.json"

    /** Une synthèse plus vieille que ça est signalée comme « pas à jour ». */
    const val SYNTHESE_PERIMEE_HEURES = 36L

    /** Temps passé hors de l'appli avant de redemander l'empreinte. */
    const val VERROU_APRES_MS = 60_000L

    val SCOPES = listOf(
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    )
}
